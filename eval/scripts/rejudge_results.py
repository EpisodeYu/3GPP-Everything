"""M8 baseline — Judge-only 重跑（不重跑 agent）。

输入：已有 results.json（带 answer + citations） + 新 golden（v1.yaml 含 M7.7+
修正） + BM25 by_spec/*.jsonl 用作 chunk content 查找源。

输出 eval-results/m8-baseline/{results.json, report.md}：
- 重算的 substring 指标（fact_coverage / forbidden_violations / spec_recall /
  section_recall）—— 对新 golden
- 用 **deepseek-v4-pro** 重跑的 ragas 4 metric（去掉 GLM-5.1 时期判分污染）
- 用 **deepseek-v4-pro** 重跑的 negative_judge_verdict
- 保留：agent answer / citations / retrieved_specs / retrieved_sections /
  latency / cost / terminal_event（这些都是 agent 跑出来的，不重判）

为什么 contexts 不空？citations 里没 content 字段，原 ragas 是降级到
"spec_id §section" 占位串打分（不准确）。本脚本读 BM25 by_spec jsonl
重建 chunk_id → content 映射，把 content 注入 citations 后再喂给 ragas。

用法：

    cd /home/s1yu/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.rejudge_results \\
            --results eval-results/m7-post-m75-baseline/results.json \\
            --golden eval/golden/v1.yaml \\
            --bm25-dir /data/tgpp/bm25/voyage \\
            --out-dir eval-results/m8-baseline \\
            --run-label m8-baseline-2026-05-24
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from statistics import mean
from typing import Any

from eval.langfuse_dataset import (
    get_client,
    make_eval_trace_id,
    push_run_score,
)
from eval.negative_judge import NegativeJudge, build_default_negative_judge
from eval.ragas_eval import RagasScorer, build_default_ragas_scorer
from eval.runner import AgentResponse
from eval.runner_retrieval import GoldenItem, load_golden
from eval.settings import EvalSettings

log = logging.getLogger("rejudge")


# === Substring metrics（与 runner._fact_coverage / _forbidden_violations 同步）====


def _fact_coverage(answer: str, facts: Iterable[Any]) -> float | None:
    fs = [str(f).strip() for f in (facts or []) if str(f).strip()]
    if not fs:
        return None
    hay = (answer or "").lower()
    return sum(1 for f in fs if f.lower() in hay) / len(fs)


def _forbidden_hits(answer: str, forbidden: Iterable[Any]) -> list[str]:
    hay = (answer or "").lower()
    return [str(f) for f in (forbidden or []) if str(f).strip() and str(f).lower() in hay]


def _spec_match(expected_specs: list[Any], retrieved_specs: list[str]) -> float | None:
    exp = [str(es.get("spec_id", "")) for es in (expected_specs or []) if isinstance(es, dict)]
    exp = [e for e in exp if e]
    if not exp:
        return None
    ret = {str(s) for s in retrieved_specs or []}
    return 1.0 if any(e in ret for e in exp) else 0.0


def _section_match(item: GoldenItem, retrieved_sections: list[str]) -> float | None:
    """retrieved_sections 是 "spec §clause" 字符串列表；用与 retrieval 端一致的前缀语义。"""
    if not item.expected_specs:
        return None
    expected: list[tuple[str, tuple[str, ...]]] = []
    for es in item.expected_specs:
        for sec in es.sections:
            segs = tuple(s for s in str(sec).split(".") if s)
            expected.append((es.spec_id, segs))
    # 从 retrieved_sections 提取裸 spec_ids（fallback + 整体 spec 匹配都需要）
    retrieved_spec_ids = []
    for rs in retrieved_sections or []:
        rs_s = str(rs)
        retrieved_spec_ids.append(rs_s.split(" §", 1)[0].strip() if " §" in rs_s else rs_s)
    if not expected:
        # expected_specs 存在但无 sections → fallback 到 spec_match
        return _spec_match(
            [{"spec_id": e.spec_id} for e in item.expected_specs], retrieved_spec_ids
        )
    # 解析 retrieved_sections 形如 "23.501 §5.2.1" 或裸 spec
    for rs in retrieved_sections or []:
        rs_s = str(rs)
        if " §" in rs_s:
            sid, clause = rs_s.split(" §", 1)
        else:
            sid, clause = rs_s, ""
        sid = sid.strip()
        clause_segs = tuple(c for c in clause.split(".") if c)
        for exp_sid, exp_segs in expected:
            if sid != exp_sid:
                continue
            if not exp_segs:
                return 1.0
            if len(exp_segs) > len(clause_segs):
                continue
            if all(a == b for a, b in zip(exp_segs, clause_segs[: len(exp_segs)], strict=True)):
                return 1.0
    return 0.0


# === BM25 chunk content lookup ============================================


def build_chunk_content_index(bm25_dir: Path, *, needed_specs: set[str]) -> dict[str, str]:
    """对 needed_specs 加载 by_spec/{spec}.jsonl，构 {chunk_id: content}."""
    out: dict[str, str] = {}
    by_spec = bm25_dir / "by_spec"
    if not by_spec.is_dir():
        raise FileNotFoundError(f"BM25 dir not found: {by_spec}")
    for sid in sorted(needed_specs):
        p = by_spec / f"{sid}.jsonl"
        if not p.exists():
            log.warning("BM25 file missing for spec=%s", sid)
            continue
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = str(rec.get("chunk_id") or "")
                if cid and cid not in out:
                    out[cid] = str(rec.get("content") or "")
    log.info("loaded chunk content for %d chunk_ids across %d specs", len(out), len(needed_specs))
    return out


def _hydrate_citations(
    citations: list[dict], chunk_idx: dict[str, str]
) -> list[dict]:
    """给 citations 注入 content 字段（从 chunk_idx 查；缺失时空字符串保留 placeholder fallback）."""
    out: list[dict] = []
    for c in citations or []:
        nc = dict(c)
        cid = str(c.get("chunk_id") or "")
        if cid in chunk_idx:
            nc["content"] = chunk_idx[cid]
        out.append(nc)
    return out


# === Rejudge core =========================================================


def _build_agent_response(row: dict, hydrated_citations: list[dict]) -> AgentResponse:
    return AgentResponse(
        answer=row.get("answer", "") or "",
        citations=hydrated_citations,
        # No chunks_rerank/hit field in stored results.json; ragas falls back to citations
        chunks_hit=[],
        chunks_rerank=[],
        terminal_event=row.get("terminal_event", "final"),
        duration_ms=int(row.get("duration_ms") or 0),
    )


def _do_ragas(
    scorer: RagasScorer | None, item: GoldenItem, resp: AgentResponse
) -> dict[str, float | None]:
    if scorer is None:
        return {
            "ragas_faithfulness": None,
            "ragas_answer_relevance": None,
            "ragas_context_recall": None,
            "ragas_context_precision": None,
        }
    try:
        return scorer.score_item(item, resp)
    except Exception as exc:
        log.warning("ragas score_item failed for %s: %s", item.id, exc)
        return {
            "ragas_faithfulness": None,
            "ragas_answer_relevance": None,
            "ragas_context_recall": None,
            "ragas_context_precision": None,
        }


def _do_negative_judge(
    judge: NegativeJudge | None, item: GoldenItem, resp: AgentResponse
) -> tuple[str | None, str | None]:
    if judge is None or not item.must_say_not_found:
        return None, None
    try:
        out = judge.score_item(item, resp)
        return out.get("verdict") or None, out.get("reason") or None
    except Exception as exc:
        log.warning("negative_judge failed for %s: %s", item.id, exc)
        return None, str(exc)[:200]


def _aggregate(rows: list[dict]) -> dict[str, Any]:
    def _safe_mean(xs: list[Any]) -> float | None:
        v = [x for x in xs if isinstance(x, (int, float)) and x == x]
        return mean(v) if v else None

    n = len(rows)
    return {
        "n": n,
        "context_recall_spec": _safe_mean([r.get("context_recall_spec") for r in rows]),
        "context_recall_section": _safe_mean([r.get("context_recall_section") for r in rows]),
        "fact_coverage": _safe_mean([r.get("fact_coverage") for r in rows]),
        "forbidden_hit_rate": (
            sum(1 for r in rows if r.get("forbidden_violations")) / n if n else 0.0
        ),
        "ragas_faithfulness": _safe_mean([r.get("ragas_faithfulness") for r in rows]),
        "ragas_answer_relevance": _safe_mean([r.get("ragas_answer_relevance") for r in rows]),
        "ragas_context_recall": _safe_mean([r.get("ragas_context_recall") for r in rows]),
        "ragas_context_precision": _safe_mean([r.get("ragas_context_precision") for r in rows]),
        "duration_p50_ms": (
            statistics.median([r.get("duration_ms") for r in rows if r.get("duration_ms")])
            if any(r.get("duration_ms") for r in rows)
            else None
        ),
        "duration_p90_ms": (
            statistics.quantiles(
                [r.get("duration_ms") for r in rows if r.get("duration_ms")], n=10
            )[8]
            if sum(1 for r in rows if r.get("duration_ms")) >= 10
            else None
        ),
    }


def _negative_pass_rate(rows: list[dict]) -> dict[str, Any]:
    neg = [r for r in rows if r.get("category") == "negative"]
    n = len(neg)
    if n == 0:
        return {"n": 0}
    valid = sum(1 for r in neg if r.get("negative_judge_verdict") == "VALID_REFUSAL")
    partial = sum(1 for r in neg if r.get("negative_judge_verdict") == "PARTIAL_REFUSAL")
    invalid = sum(1 for r in neg if r.get("negative_judge_verdict") == "INVALID")
    weighted = (valid + 0.5 * partial) / n
    return {
        "n": n,
        "valid": valid,
        "partial": partial,
        "invalid": invalid,
        "weighted_pass_rate": weighted,
    }


# === Report writing =======================================================


def _write_report(out_dir: Path, agg: dict, by_source: dict, by_category: dict, neg: dict) -> None:
    lines: list[str] = []
    lines.append("# M8 Baseline — Judge-only Rejudge 报告")
    lines.append("")
    lines.append(f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("- 不重跑 agent；仅在已有 answer + 重新 hydrate 的 contexts 上跑 ragas + negative_judge")
    lines.append("- judge LLM: **deepseek-v4-pro** (M7.7 起替代 GLM-5.1，单价 -50%/-75%)")
    lines.append("")
    lines.append("## 整体")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---:|")
    for k, v in agg.items():
        if v is None:
            lines.append(f"| {k} | — |")
        elif isinstance(v, float):
            lines.append(f"| {k} | {v:.3f} |")
        else:
            lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## negative pass rate")
    lines.append("")
    if neg.get("n"):
        lines.append(
            f"- weighted_pass_rate = **{neg['weighted_pass_rate']:.3f}** "
            f"({neg['valid']} VALID + {neg['partial']} PARTIAL + {neg['invalid']} INVALID / {neg['n']})"
        )
    lines.append("")
    lines.append("## by source")
    lines.append("")
    lines.append(
        "| source | n | spec_recall | section_recall | fact_cov | faith | answer_rel | ctx_recall | ctx_prec |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for src, a in sorted(by_source.items()):
        lines.append(
            f"| {src} | {a['n']} | "
            f"{_fmt(a.get('context_recall_spec'))} | {_fmt(a.get('context_recall_section'))} | "
            f"{_fmt(a.get('fact_coverage'))} | "
            f"{_fmt(a.get('ragas_faithfulness'))} | {_fmt(a.get('ragas_answer_relevance'))} | "
            f"{_fmt(a.get('ragas_context_recall'))} | {_fmt(a.get('ragas_context_precision'))} |"
        )
    lines.append("")
    lines.append("## by category")
    lines.append("")
    lines.append(
        "| category | n | spec_recall | section_recall | fact_cov | faith | answer_rel |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for cat, a in sorted(by_category.items()):
        lines.append(
            f"| {cat} | {a['n']} | "
            f"{_fmt(a.get('context_recall_spec'))} | {_fmt(a.get('context_recall_section'))} | "
            f"{_fmt(a.get('fact_coverage'))} | "
            f"{_fmt(a.get('ragas_faithfulness'))} | {_fmt(a.get('ragas_answer_relevance'))} |"
        )
    lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


# === Main =================================================================


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path, help="原 results.json 路径")
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--bm25-dir", type=Path, default=Path("/data/tgpp/bm25/voyage"))
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--run-label", default="m8-baseline")
    ap.add_argument("--skip-ragas", action="store_true", help="只重跑 negative_judge + substring")
    ap.add_argument("--skip-negative", action="store_true")
    ap.add_argument("--push-langfuse", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help=">0 = 仅前 N 题 (debug)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    with args.results.open() as f:
        orig = json.load(f)
    items_by_id: dict[str, GoldenItem] = {it.id: it for it in load_golden(args.golden)}

    rows = orig["results"]
    if args.limit > 0:
        rows = rows[: args.limit]

    needed_specs: set[str] = set()
    for r in rows:
        for c in r.get("citations") or []:
            if sid := c.get("spec_id"):
                needed_specs.add(sid)

    chunk_idx = build_chunk_content_index(args.bm25_dir, needed_specs=needed_specs)

    settings = EvalSettings()
    if not settings.litellm_api_key:
        print("ERROR: LITELLM_API_KEY missing", file=sys.stderr)
        return 2

    scorer: RagasScorer | None = None
    if not args.skip_ragas:
        try:
            scorer = build_default_ragas_scorer(settings)
        except Exception as exc:
            log.warning("ragas init failed; ragas disabled: %s", exc)

    judge: NegativeJudge | None = None
    if not args.skip_negative:
        try:
            judge = build_default_negative_judge(settings)
        except Exception as exc:
            log.warning("negative_judge init failed; disabled: %s", exc)

    lf_client = get_client(settings) if args.push_langfuse else None
    if args.push_langfuse and lf_client is None:
        log.warning("--push-langfuse set but client unavailable; skipping LF upload")

    new_rows: list[dict] = []
    for i, row in enumerate(rows, start=1):
        gid = row["item_id"]
        item = items_by_id.get(gid)
        if not item:
            log.warning("item %s missing from new golden; skipping", gid)
            continue

        # Substring metrics on NEW golden
        ret_specs = row.get("retrieved_specs") or []
        ret_sections = row.get("retrieved_sections") or []
        new_spec_r = _spec_match([asdict_or_dict(es) for es in item.expected_specs], ret_specs)
        new_section_r = _section_match(item, ret_sections)
        new_fc = _fact_coverage(row.get("answer", ""), item.expected_facts)
        new_fb = _forbidden_hits(row.get("answer", ""), item.forbidden)

        # Rebuild agent_response with hydrated content for ragas
        hydrated = _hydrate_citations(row.get("citations") or [], chunk_idx)
        resp = _build_agent_response(row, hydrated)

        # Ragas
        ragas_scores: dict[str, float | None] = {}
        if scorer and (item.expected_specs or item.expected_facts):
            ragas_scores = _do_ragas(scorer, item, resp)

        # Negative judge
        nj_verdict, nj_reason = _do_negative_judge(judge, item, resp)

        new_row = {
            **row,
            "context_recall_spec": new_spec_r,
            "context_recall_section": new_section_r,
            "fact_coverage": new_fc,
            "forbidden_violations": new_fb,
            "negative_judge_verdict": nj_verdict
            if nj_verdict is not None
            else row.get("negative_judge_verdict"),
            "negative_judge_reason": nj_reason
            if nj_reason is not None
            else row.get("negative_judge_reason"),
            **ragas_scores,
        }
        new_rows.append(new_row)

        # Langfuse push
        if lf_client is not None:
            trace_id = make_eval_trace_id(args.run_label, gid, client=lf_client)
            if trace_id:
                push_run_score(
                    trace_id,
                    {
                        "context_recall_spec": new_row.get("context_recall_spec"),
                        "context_recall_section": new_row.get("context_recall_section"),
                        "fact_coverage": new_row.get("fact_coverage"),
                        "forbidden_violation": 1.0
                        if new_row.get("forbidden_violations")
                        else 0.0,
                        "ragas_faithfulness": new_row.get("ragas_faithfulness"),
                        "ragas_answer_relevance": new_row.get("ragas_answer_relevance"),
                        "ragas_context_recall": new_row.get("ragas_context_recall"),
                        "ragas_context_precision": new_row.get("ragas_context_precision"),
                        "negative_judge_valid": (
                            1.0
                            if new_row.get("negative_judge_verdict") == "VALID_REFUSAL"
                            else 0.0
                            if new_row.get("negative_judge_verdict") == "INVALID"
                            else 0.5
                            if new_row.get("negative_judge_verdict") == "PARTIAL_REFUSAL"
                            else None
                        ),
                    },
                    comment="M8 baseline rejudge",
                    metadata={"item_id": gid, "category": item.category, "source": item.source},
                    client=lf_client,
                )
        if i % 10 == 0:
            log.info("rejudged %d/%d", i, len(rows))

    # Aggregate
    agg = _aggregate(new_rows)
    by_source: dict[str, dict] = {}
    by_category: dict[str, dict] = {}
    src_buckets: dict[str, list[dict]] = defaultdict(list)
    cat_buckets: dict[str, list[dict]] = defaultdict(list)
    for r in new_rows:
        item = items_by_id.get(r["item_id"])
        if not item:
            continue
        src_buckets[item.source].append(r)
        cat_buckets[item.category].append(r)
    for src, rs in src_buckets.items():
        by_source[src] = _aggregate(rs)
    for cat, rs in cat_buckets.items():
        by_category[cat] = _aggregate(rs)
    neg = _negative_pass_rate(new_rows)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(
            {
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "total_items_in_golden": len(items_by_id),
                "unique_results": len(new_rows),
                "overall": agg,
                "by_source": by_source,
                "by_category": by_category,
                "negative_summary": neg,
                "results": new_rows,
                "_meta": {
                    "judge_model": "deepseek-v4-pro",
                    "rejudged_from": str(args.results),
                    "golden_version": str(args.golden),
                },
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    _write_report(out_dir, agg, by_source, by_category, neg)
    print(f"wrote: {out_dir / 'results.json'}")
    print(f"wrote: {out_dir / 'report.md'}")
    print(f"overall faith={_fmt(agg.get('ragas_faithfulness'))} "
          f"spec_recall={_fmt(agg.get('context_recall_spec'))} "
          f"fact_cov={_fmt(agg.get('fact_coverage'))} "
          f"neg_pass={_fmt(neg.get('weighted_pass_rate'))}")
    return 0


def asdict_or_dict(es) -> dict:
    """ExpectedSpec dataclass → dict (for _spec_match)."""
    return {"spec_id": es.spec_id, "sections": list(es.sections)}


if __name__ == "__main__":
    raise SystemExit(main())
