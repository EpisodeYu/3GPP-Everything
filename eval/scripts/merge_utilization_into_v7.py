"""把 compute_utilization.py 跑出的 utilization 分数合并进 v7 results.json，
产出 v8（即 v7 + metric swap）。

不重跑 ragas；直接读 /tmp/ragas-work/util-full.log 解析 `hand-X cat old -> util +delta` 行，
将 utilization 值写到 results.json 的 ragas_context_precision 字段，重算 overall/by_category。

跑法：
    cd /data/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.merge_utilization_into_v7 \\
        --src eval-results/v7-sent-gt-20260529T234652Z/results.json \\
        --log /tmp/ragas-work/util-full.log \\
        --out-dir eval-results/v8-sent-utilization-2026-05-30
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

LINE_RE = re.compile(r"^(?P<item_id>hand-[\w-]+)\s+\S+\s+\S+\s*->\s*(?P<util>[\d.]+)\s+")


def parse_log(p: Path) -> dict[str, float]:
    """从 compute_utilization 的 log 抓 item_id → util 值。"""
    out: dict[str, float] = {}
    for line in p.read_text().splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        try:
            out[m.group("item_id")] = float(m.group("util"))
        except ValueError:
            continue
    return out


def _safe_mean(xs: list[Any]) -> float | None:
    v = [x for x in xs if isinstance(x, (int, float)) and x == x]
    return mean(v) if v else None


def _aggregate(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    return {
        "n": n,
        "context_recall_spec": _safe_mean([r.get("context_recall_spec") for r in rows]),
        "context_recall_section": _safe_mean([r.get("context_recall_section") for r in rows]),
        "fact_coverage": _safe_mean([r.get("fact_coverage") for r in rows]),
        "fact_coverage_substring": _safe_mean([r.get("fact_coverage_substring") for r in rows]),
        "fact_coverage_judge": _safe_mean([r.get("fact_coverage_judge") for r in rows]),
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


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def _write_report(
    out_dir: Path,
    agg: dict,
    by_source: dict,
    by_category: dict,
    neg: dict,
    *,
    note: str = "",
) -> None:
    lines: list[str] = []
    lines.append("# v8 ragas baseline — SENT ground_truth + ContextUtilization metric swap")
    lines.append("")
    if note:
        lines.append(note)
        lines.append("")
    lines.append("## 整体")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---:|")
    for k, v in agg.items():
        lines.append(f"| {k} | {_fmt(v)} |")
    lines.append("")
    lines.append("## negative pass rate")
    lines.append("")
    if neg.get("n"):
        lines.append(
            f"- weighted_pass_rate = **{neg['weighted_pass_rate']:.3f}** "
            f"({neg['valid']} VALID + {neg['partial']} PARTIAL + {neg['invalid']} INVALID / {neg['n']})"
        )
    lines.append("")
    lines.append("## by category")
    lines.append("")
    lines.append(
        "| category | n | sec_recall | fact_cov | faith | answer_rel | ctx_recall | ctx_prec(util) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for cat, a in sorted(by_category.items()):
        lines.append(
            f"| {cat} | {a['n']} | "
            f"{_fmt(a.get('context_recall_section'))} | "
            f"{_fmt(a.get('fact_coverage'))} | "
            f"{_fmt(a.get('ragas_faithfulness'))} | "
            f"{_fmt(a.get('ragas_answer_relevance'))} | "
            f"{_fmt(a.get('ragas_context_recall'))} | "
            f"{_fmt(a.get('ragas_context_precision'))} |"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args()

    util_map = parse_log(args.log)
    print(f"parsed {len(util_map)} utilization scores from log")

    doc = json.loads(args.src.read_text())
    rows = doc["results"]

    n_updated = 0
    for r in rows:
        gid = r["item_id"]
        if gid in util_map:
            r["ragas_context_precision"] = util_map[gid]
            n_updated += 1
    print(f"updated {n_updated}/{len(rows)} rows")

    overall = _aggregate(rows)
    src_b: dict[str, list] = defaultdict(list)
    cat_b: dict[str, list] = defaultdict(list)
    for r in rows:
        cat_b[r["category"]].append(r)
        # category 即 source 的代理（手写题都是 hand_crafted）；按现有 schema 直接复用
        src_b["hand_crafted"].append(r)
    by_source = {s: _aggregate(rs) for s, rs in src_b.items()}
    by_category = {c: _aggregate(rs) for c, rs in cat_b.items()}
    neg = _negative_pass_rate(rows)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "results.json").write_text(
        json.dumps(
            {
                **doc,
                "overall": overall,
                "by_source": by_source,
                "by_category": by_category,
                "negative_summary": neg,
                "_meta": {
                    **(doc.get("_meta") or {}),
                    "ragas_context_precision_method": "ContextUtilization (without reference)",
                    "ragas_context_precision_log": str(args.log),
                    "merged_from_src": str(args.src),
                },
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    note = (
        f"- 输入：v7 (`{args.src}`) + utilization 重算 (`{args.log}`)\n"
        "- ragas_context_precision 字段从 ContextPrecision(with reference)\n"
        "  切换到 ContextUtilization(without reference)；其他 metric 保持 v7\n"
    )
    _write_report(args.out_dir, overall, by_source, by_category, neg, note=note)
    print(f"wrote {args.out_dir / 'results.json'}")
    print(f"wrote {args.out_dir / 'report.md'}")
    print(
        f"overall faith={_fmt(overall.get('ragas_faithfulness'))} "
        f"ans_rel={_fmt(overall.get('ragas_answer_relevance'))} "
        f"ctx_recall={_fmt(overall.get('ragas_context_recall'))} "
        f"ctx_prec={_fmt(overall.get('ragas_context_precision'))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
