"""把两次 ragas rejudge 的 results.json 合并：per-item 取 max（per metric），
减少 judge 单次随机性的影响。

用法：
    cd /data/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.merge_two_runs \\
        --src1 eval-results/v8-utilization-majvote-20260530T125035Z/results.json \\
        --src2 eval-results/v9-golden-aliases-20260530T170223Z/results.json \\
        --out-dir eval-results/v9b-v8plus9-max
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from statistics import mean

METRIC_FIELDS = (
    "ragas_faithfulness",
    "ragas_answer_relevance",
    "ragas_context_recall",
    "ragas_context_precision",
)


def _safe_mean(xs):
    v = [x for x in xs if isinstance(x, (int, float)) and x == x]
    return mean(v) if v else None


def _aggregate(rows):
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
    }


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src1", required=True, type=Path)
    ap.add_argument("--src2", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument(
        "--strategy",
        default="max",
        choices=["max", "mean", "second_unless_none"],
        help="max=per-item max; mean=average of two; second_unless_none=用 src2，None 时回退 src1",
    )
    args = ap.parse_args()

    d1 = json.loads(args.src1.read_text())
    d2 = json.loads(args.src2.read_text())
    m1 = {r["item_id"]: r for r in d1["results"]}
    m2 = {r["item_id"]: r for r in d2["results"]}

    merged_rows = []
    for k in m1:
        r1 = m1[k]
        r2 = m2.get(k, {})
        merged = dict(r1)  # base on src1
        # also keep src2 derived fields for substring (use latest)
        for fld in (
            "context_recall_spec",
            "context_recall_section",
            "fact_coverage",
            "fact_coverage_substring",
            "fact_coverage_judge",
            "forbidden_violations",
        ):
            if fld in r2:
                merged[fld] = r2[fld]
        # merge ragas
        for f in METRIC_FIELDS:
            v1 = r1.get(f)
            v2 = r2.get(f)
            if args.strategy == "max":
                pool = [x for x in (v1, v2) if isinstance(x, (int, float)) and x == x]
                merged[f] = max(pool) if pool else None
            elif args.strategy == "mean":
                pool = [x for x in (v1, v2) if isinstance(x, (int, float)) and x == x]
                merged[f] = (sum(pool) / len(pool)) if pool else None
            elif args.strategy == "second_unless_none":
                merged[f] = v2 if isinstance(v2, (int, float)) and v2 == v2 else v1
        merged_rows.append(merged)

    overall = _aggregate(merged_rows)
    cat_b = defaultdict(list)
    for r in merged_rows:
        cat_b[r.get("category", "?")].append(r)
    by_category = {c: _aggregate(rs) for c, rs in cat_b.items()}

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "results.json").write_text(
        json.dumps(
            {
                "_meta": {
                    "merge_strategy": args.strategy,
                    "src1": str(args.src1),
                    "src2": str(args.src2),
                    "ragas_context_precision_method": "ContextUtilization (without reference)",
                    "answer_relevance_method": "ragas AnswerRelevancy + majority-vote noncommittal",
                },
                "overall": overall,
                "by_category": by_category,
                "results": merged_rows,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )

    # write report
    lines = [
        f"# Merged ragas baseline (strategy={args.strategy})",
        "",
        f"- src1: `{args.src1}`",
        f"- src2: `{args.src2}`",
        "",
        "## overall",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
    ]
    for k, v in overall.items():
        lines.append(f"| {k} | {_fmt(v)} |")
    lines.append("")
    lines.append("## by category")
    lines.append("")
    lines.append("| cat | n | faith | ans_rel | ctx_recall | ctx_prec |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for c, a in sorted(by_category.items()):
        if c == "negative":
            continue
        lines.append(
            f"| {c} | {a['n']} | "
            f"{_fmt(a.get('ragas_faithfulness'))} | "
            f"{_fmt(a.get('ragas_answer_relevance'))} | "
            f"{_fmt(a.get('ragas_context_recall'))} | "
            f"{_fmt(a.get('ragas_context_precision'))} |"
        )
    (args.out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"wrote {args.out_dir / 'results.json'}")
    print(f"wrote {args.out_dir / 'report.md'}")
    print(
        f"overall (strategy={args.strategy}): faith={_fmt(overall.get('ragas_faithfulness'))} "
        f"ans_rel={_fmt(overall.get('ragas_answer_relevance'))} "
        f"ctx_recall={_fmt(overall.get('ragas_context_recall'))} "
        f"ctx_prec={_fmt(overall.get('ragas_context_precision'))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
