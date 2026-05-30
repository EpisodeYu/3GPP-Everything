"""消融实验：对几道代表性问题，尝试不同的 ground_truth 构造方式，
观察 ragas context_recall / context_precision / faithfulness 的变化。

变体：
  ORIG    : ' '.join(facts)            （现状）
  SENT    : '. '.join(facts) + '.'      （强制 sentence 边界）
  WRAPPED : 'The answer should mention X; The answer should mention Y; ...'  （英文 wrap）
  SECTION : 用 expected 章节的 BM25 chunk 内容作为 reference
  HYBRID  : facts 包装句 + 章节内容拼接 (section + 关键事实双保险)

每个变体对每题跑一次 ragas 4 metric，输出对比表。

跑法：
    cd /data/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.ablate_ground_truth \\
            --results eval-results/v6-citation-index-20260529T092708Z-ragas/results.json \\
            --golden eval/golden/v1.yaml \\
            --bm25-dir /data/tgpp/bm25/voyage \\
            --item-ids hand-multi-002,hand-multi-007,hand-table-008,hand-formula-001,hand-formula-006,hand-table-002,hand-formula-008
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
from pathlib import Path

from eval.ragas_eval import _extract_contexts, build_default_ragas_scorer
from eval.runner_retrieval import load_golden
from eval.scripts.rejudge_results import (
    _build_agent_response,
    _hydrate_citations,
    build_chunk_content_index,
)
from eval.settings import EvalSettings

log = logging.getLogger("ablate_gt")


def _gt_orig(item) -> str:
    return " ".join(item.expected_facts) if item.expected_facts else "(no ground truth)"


def _gt_sent(item) -> str:
    if not item.expected_facts:
        return "(no ground truth)"
    return ". ".join(str(f).strip() for f in item.expected_facts) + "."


def _gt_wrapped(item) -> str:
    if not item.expected_facts:
        return "(no ground truth)"
    return (
        ". ".join(f"The answer should mention {str(f).strip()}" for f in item.expected_facts) + "."
    )


def _section_content(item, chunk_idx_by_spec: dict[str, list[dict]]) -> str:
    """拼接所有 expected sections 的 BM25 chunk 内容。"""
    out = []
    for es in item.expected_specs:
        chunks = chunk_idx_by_spec.get(es.spec_id, [])
        for sec in es.sections:
            sec_str = str(sec).strip()
            # 同前缀 clause 都吃进来（"4.4.4" 命中 "4.4.4", "4.4.4.2" 等）
            sec_parts = sec_str.split(".")
            for c in chunks:
                clause = str(c.get("clause", ""))
                cparts = clause.split(".") if clause else []
                if len(cparts) < len(sec_parts):
                    continue
                if cparts[: len(sec_parts)] == sec_parts:
                    out.append(c.get("content", ""))
    # dedup + truncate
    seen = set()
    uniq = []
    for s in out:
        h = hash(s)
        if h in seen:
            continue
        seen.add(h)
        uniq.append(s)
    text = "\n\n---\n\n".join(uniq)
    # truncate to ~3000 chars to avoid blow up statement count
    return text[:3000]


def _gt_section(item, chunk_idx_by_spec: dict[str, list[dict]]) -> str:
    sec = _section_content(item, chunk_idx_by_spec)
    if sec:
        return sec
    return _gt_sent(item)


def _gt_hybrid(item, chunk_idx_by_spec: dict[str, list[dict]]) -> str:
    sec = _section_content(item, chunk_idx_by_spec)
    facts_part = ". ".join(
        f"The answer should mention {str(f).strip()}" for f in (item.expected_facts or [])
    )
    if facts_part:
        facts_part += "."
    if sec and facts_part:
        return f"{facts_part}\n\nReference section content:\n{sec}"
    return sec or facts_part or "(no ground truth)"


VARIANTS = ("ORIG", "SENT", "WRAPPED", "SECTION", "HYBRID")


def _build_chunks_by_spec(bm25_dir: Path, specs: set[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for sid in sorted(specs):
        p = bm25_dir / "by_spec" / f"{sid}.jsonl"
        if not p.exists():
            continue
        rows = []
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    rows.append(json.loads(line))
        out[sid] = rows
    return out


def _score(scorer, item, resp, gt: str, *, metrics_filter: set[str] | None = None) -> dict:
    """跑指定子集 metric；默认仅 context_recall (最便宜，最相关)。"""
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )

    contexts = _extract_contexts(resp)
    if not contexts or not resp.answer:
        return {
            "context_recall": None,
            "context_precision": None,
            "faithfulness": None,
            "answer_relevance": None,
        }
    row = {
        "user_input": item.question,
        "response": resp.answer,
        "retrieved_contexts": contexts,
        "reference": gt,
        "question": item.question,
        "answer": resp.answer,
        "contexts": contexts,
        "ground_truth": gt,
    }
    available = {
        "context_recall": context_recall,
        "context_precision": context_precision,
        "faithfulness": faithfulness,
        "answer_relevance": answer_relevancy,
    }
    if metrics_filter:
        metrics_list = [available[m] for m in metrics_filter if m in available]
    else:
        metrics_list = [context_recall]
    try:
        ds = Dataset.from_list([row])
        ev = evaluate(
            ds,
            metrics=metrics_list,
            llm=scorer.llm,
            embeddings=scorer.embeddings,
            raise_exceptions=False,
            show_progress=False,
        )
        s = next(iter(ev.scores)) if ev.scores else {}
        return {
            "faithfulness": s.get("faithfulness"),
            "answer_relevance": s.get("answer_relevancy"),
            "context_recall": s.get("context_recall"),
            "context_precision": s.get("context_precision"),
        }
    except Exception as exc:
        log.warning("evaluate failed: %s", exc)
        return {
            "context_recall": None,
            "context_precision": None,
            "faithfulness": None,
            "answer_relevance": None,
        }


def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--bm25-dir", type=Path, default=Path("/data/tgpp/bm25/voyage"))
    ap.add_argument("--item-ids", required=True)
    ap.add_argument(
        "--metrics",
        default="context_recall",
        help="逗号分隔；可选 context_recall,context_precision,faithfulness,answer_relevance",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s"
    )

    ids = [s.strip() for s in args.item_ids.split(",") if s.strip()]
    doc = json.loads(args.results.read_text())
    items_by_id = {it.id: it for it in load_golden(args.golden)}
    target_rows = [r for r in doc["results"] if r["item_id"] in ids]
    if not target_rows:
        print("no matching items")
        return

    # build chunk content idx for citation hydration
    needed_specs = {
        c.get("spec_id")
        for r in target_rows
        for c in (r.get("citations") or [])
        if c.get("spec_id")
    }
    chunk_idx = build_chunk_content_index(
        args.bm25_dir, needed_specs={s for s in needed_specs if s}
    )

    # build by-spec lookup for section-content extraction
    expected_specs: set[str] = set()
    for r in target_rows:
        item = items_by_id[r["item_id"]]
        for es in item.expected_specs:
            expected_specs.add(es.spec_id)
    chunks_by_spec = _build_chunks_by_spec(args.bm25_dir, expected_specs)

    settings = EvalSettings()
    scorer = build_default_ragas_scorer(settings)
    metrics_filter = {m.strip() for m in args.metrics.split(",") if m.strip()}

    rows = []
    for r in target_rows:
        item = items_by_id[r["item_id"]]
        hydrated = _hydrate_citations(r.get("citations") or [], chunk_idx)
        resp = _build_agent_response(r, hydrated)
        # original score baseline
        orig_ragas = {
            "context_recall": r.get("ragas_context_recall"),
            "context_precision": r.get("ragas_context_precision"),
            "faithfulness": r.get("ragas_faithfulness"),
            "answer_relevance": r.get("ragas_answer_relevance"),
        }
        per_variant = {"BASELINE": orig_ragas}
        for v in VARIANTS:
            if v == "ORIG":
                gt = _gt_orig(item)
            elif v == "SENT":
                gt = _gt_sent(item)
            elif v == "WRAPPED":
                gt = _gt_wrapped(item)
            elif v == "SECTION":
                gt = _gt_section(item, chunks_by_spec)
            elif v == "HYBRID":
                gt = _gt_hybrid(item, chunks_by_spec)
            else:
                gt = _gt_orig(item)
            print(f"\n→ {item.id} variant={v}; gt[:200]={gt[:200]!r}", flush=True)
            scores = _score(scorer, item, resp, gt, metrics_filter=metrics_filter)
            per_variant[v] = scores
            print(f"    scores: {scores}", flush=True)
        rows.append({"item_id": item.id, "category": item.category, **per_variant})

    # Compact summary table
    print("\n\n==== SUMMARY (context_recall) ====")
    header = ["item_id", "BASELINE", *list(VARIANTS)]
    print("\t".join(header))
    for r in rows:
        cells = [r["item_id"]]
        for k in ["BASELINE", *list(VARIANTS)]:
            v = (r.get(k) or {}).get("context_recall")
            cells.append(f"{v:.2f}" if isinstance(v, (int, float)) else "—")
        print("\t".join(cells))

    print("\n==== SUMMARY (context_precision) ====")
    print("\t".join(header))
    for r in rows:
        cells = [r["item_id"]]
        for k in ["BASELINE", *list(VARIANTS)]:
            v = (r.get(k) or {}).get("context_precision")
            cells.append(f"{v:.2f}" if isinstance(v, (int, float)) else "—")
        print("\t".join(cells))

    print("\n==== SUMMARY (faithfulness) ====")
    print("\t".join(header))
    for r in rows:
        cells = [r["item_id"]]
        for k in ["BASELINE", *list(VARIANTS)]:
            v = (r.get(k) or {}).get("faithfulness")
            cells.append(f"{v:.2f}" if isinstance(v, (int, float)) else "—")
        print("\t".join(cells))

    print("\n==== SUMMARY (answer_relevance) ====")
    print("\t".join(header))
    for r in rows:
        cells = [r["item_id"]]
        for k in ["BASELINE", *list(VARIANTS)]:
            v = (r.get(k) or {}).get("answer_relevance")
            cells.append(f"{v:.2f}" if isinstance(v, (int, float)) else "—")
        print("\t".join(cells))


if __name__ == "__main__":
    amain()
