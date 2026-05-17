"""M6 全量索引 retrieval baseline（1024 维单测）。

用途：M3 决胜固化 1024 维后，M6 把语料从 17 篇 POC 扩到 1270 篇全量。
本脚本在同一份 golden v1.yaml 上对 1024 维 collection 跑 dense-only retrieval，
并与 M3 17-spec 时段的 baseline 对比 (spec R@10 / section R@10 / MRR)，
**默认作为"信息性 baseline"输出**，不下 FAIL 结论。

口径见 ``eval-results/m6-retrieval-baseline.md``：
M6 全量 dense-only retrieval 与 M3 17-spec dense-only retrieval 不是同一草垛
（草垛扩 75×，跨 spec 干扰自然稀释 dense recall），不构成回归失败。

绕开 ``eval/runner_retrieval.py:decide_winner``（其硬 assert 双维 2048+1024）。

模式：
- ``--mode baseline``（默认）：仅报告 delta，不下 FAIL；用于 M6 全量索引完成后的初次基线
- ``--mode strict``：保留 ±2pp 容差判定；用于 M4 rerank 接上之后对照前次同口径 retrieval 评测

用法::

    PYTHONPATH=/data/3GPP-Everything uv run python eval/scripts/m6_regression.py
    PYTHONPATH=/data/3GPP-Everything uv run python eval/scripts/m6_regression.py --limit 20
    PYTHONPATH=/data/3GPP-Everything uv run python eval/scripts/m6_regression.py --mode strict
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from eval.runner_retrieval import (
    PerQuestionRow,
    evaluate_retrieval,
    load_golden,
)

log = logging.getLogger(__name__)

Mode = Literal["baseline", "strict"]

# M3 决胜 baseline（docs/03-development/06-evaluation-and-observability.md §8 + eval-results/m3-embedding-poc.md）
# 在 17 篇 POC + 119 题 golden 上的 1024 维成绩。
# 注意：与 M6 全量 dense-only 数不是同一草垛，仅作 delta 参考，不作为 FAIL 判定阈值（默认模式）。
M3_BASELINE_1024 = {
    "spec_recall@10": 0.815,
    "section_recall@10": 0.647,
    # M3 报告未单独给 1024 的 MRR 小数点后 3 位，留 None 由本次跑结果作为新 baseline
}

# strict 模式下的回归阈值（CLAUDE.md §5.6 不可降级；仅适用于 rerank 接上后同口径 retrieval 评测对照前次跑分）
REGRESSION_TOLERANCE_PP = 2.0


@dataclass(frozen=True, slots=True)
class Verdict:
    """报告口径与结论。从 metrics + mode 派生，方便单测。"""

    mode: Mode
    headline: str  # 一行结论（出现在 report.md "## 回归结论"段首）
    is_fail: bool  # strict 模式下任一指标超容差才 True；baseline 模式恒为 False
    deltas_pp: dict[str, float]  # 各指标相对 M3 baseline 的差值（百分点）


def decide_verdict(
    *,
    spec_recall_at_10: float,
    section_recall_at_10: float,
    mode: Mode,
    baseline: dict[str, float] | None = None,
    tolerance_pp: float = REGRESSION_TOLERANCE_PP,
) -> Verdict:
    """纯函数：根据 metrics + mode 决定 verdict。

    - ``mode="baseline"``：始终输出 BASELINE 行，不下 FAIL（M6 全量 dense-only 与 M3 17-spec
      dense-only 不是同一草垛，详见 ``eval-results/m6-retrieval-baseline.md``）。
    - ``mode="strict"``：任一关键指标相对 baseline 跌幅 > ``tolerance_pp`` 即 FAIL；
      用于 rerank 接上后对照前次同口径 retrieval 评测。
    """
    base = baseline or M3_BASELINE_1024
    base_sr10 = base["spec_recall@10"]
    base_secr10 = base["section_recall@10"]

    deltas_pp = {
        "spec_recall@10": (spec_recall_at_10 - base_sr10) * 100,
        "section_recall@10": (section_recall_at_10 - base_secr10) * 100,
    }

    if mode == "baseline":
        headline = (
            "**BASELINE** — M6 全量 dense-only retrieval 作为 M4 rerank ablation 的对照基线，"
            "不视作回归失败。M3 17-spec 数据在草垛扩 75× 后被自然稀释属预期；"
            "口径见 [`eval-results/m6-retrieval-baseline.md`](../../m6-retrieval-baseline.md)。"
        )
        return Verdict(mode=mode, headline=headline, is_fail=False, deltas_pp=deltas_pp)

    # strict
    is_fail = any(d < -tolerance_pp for d in deltas_pp.values())
    if is_fail:
        headline = f"**FAIL** — 任一关键指标相对前次同口径 baseline 跌幅 > {tolerance_pp:.1f}pp"
    else:
        headline = f"**PASS** — 所有关键指标在前次同口径 baseline ±{tolerance_pp:.1f}pp 内"
    return Verdict(mode=mode, headline=headline, is_fail=is_fail, deltas_pp=deltas_pp)


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--golden",
        type=Path,
        default=repo_root / "eval" / "golden" / "v1.yaml",
    )
    p.add_argument(
        "--out-root",
        type=Path,
        default=repo_root / "eval-results" / "m6-full-index",
    )
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="0 = 全跑；>0 = 只跑前 N 题（smoke 用）",
    )
    p.add_argument(
        "--mode",
        choices=("baseline", "strict"),
        default="baseline",
        help=(
            "baseline (默认) = 仅报告 delta、不下 FAIL；"
            "strict = ±2pp 容差判定（M4 rerank 接上后用，对照前次同口径 retrieval 评测）"
        ),
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def _format_diff(value: float, baseline: float | None) -> str:
    if baseline is None:
        return f"{value:.3f} (no baseline)"
    diff_pp = (value - baseline) * 100
    sign = "+" if diff_pp >= 0 else ""
    return f"{value:.3f} ({sign}{diff_pp:.2f}pp vs M3 {baseline:.3f})"


def write_report(
    *,
    out_dir: Path,
    by_dim: dict,
    rows: list[PerQuestionRow],
    golden_path: Path,
    limit: int,
    mode: Mode,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "report.md"
    json_path = out_dir / "results.json"

    dim = 1024
    d = by_dim[dim]
    m = d.metrics

    sr10 = m.spec_recall_at.get(10, 0.0)
    secr10 = m.section_recall_at.get(10, 0.0)

    verdict = decide_verdict(
        spec_recall_at_10=sr10,
        section_recall_at_10=secr10,
        mode=mode,
    )

    base_sr10 = M3_BASELINE_1024["spec_recall@10"]
    base_secr10 = M3_BASELINE_1024["section_recall@10"]

    json_path.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "scope": "m6_full_index_baseline",
                "mode": mode,
                "golden_path": str(golden_path),
                "limit": limit,
                "n_questions": m.n_questions,
                "dim": dim,
                "metrics": m.to_dict(),
                "latency_ms_p50": round(d.latency_ms_p50, 1),
                "latency_ms_p95": round(d.latency_ms_p95, 1),
                "m3_baseline_1024": M3_BASELINE_1024,
                "deltas_pp_vs_m3": verdict.deltas_pp,
                "regression_tolerance_pp": REGRESSION_TOLERANCE_PP,
                "verdict_is_fail": verdict.is_fail,
                "per_question": [
                    {
                        "item_id": r.item_id,
                        "category": r.category,
                        "expected_specs": r.expected_specs,
                        "metrics": r.metrics_by_dim.get(dim, {}),
                        "latency_ms": r.latency_ms_by_dim.get(dim, 0.0),
                    }
                    for r in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append(
        f"# M6 全量索引 dense-only retrieval baseline（dim=1024, "
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}, mode={mode}）"
    )
    lines.append("")
    lines.append(f"- golden: `{golden_path}`")
    lines.append("- collection: `tgpp_chunks_voyage_d1024`（M6 全量：1270 specs / 394,859 chunks）")
    lines.append(f"- n_questions: {m.n_questions}" + (f" (limit={limit})" if limit > 0 else ""))
    lines.append(f"- top_k: {d.metrics.n_questions and 20}")
    lines.append(f"- mode: `{mode}`（baseline = 仅报告 delta；strict = ±2pp 容差判定）")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(verdict.headline)
    lines.append("")
    lines.append(f"- spec_recall@10:    {_format_diff(sr10, base_sr10)}")
    lines.append(f"- section_recall@10: {_format_diff(secr10, base_secr10)}")
    lines.append(f"- MRR:               {m.mrr:.3f} (no M3 baseline 写入文档)")
    lines.append(f"- MRR_spec:          {m.mrr_spec:.3f}")
    lines.append("")
    lines.append("## 聚合指标")
    lines.append("")
    lines.append(
        "| dim | n | R@5 | R@10 | R@20 | spec R@10 | MRR | MRR_spec | P@10 | p50 ms | p95 ms |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    lines.append(
        f"| {dim} | {m.n_questions} | "
        f"{m.section_recall_at.get(5, 0):.3f} | "
        f"{secr10:.3f} | "
        f"{m.section_recall_at.get(20, 0):.3f} | "
        f"{sr10:.3f} | "
        f"{m.mrr:.3f} | "
        f"{m.mrr_spec:.3f} | "
        f"{m.precision_at.get(10, 0):.3f} | "
        f"{d.latency_ms_p50:.1f} | "
        f"{d.latency_ms_p95:.1f} |"
    )
    lines.append("")
    lines.append("## 按 category 分布")
    lines.append("")
    cats: dict[str, list[PerQuestionRow]] = {}
    for r in rows:
        cats.setdefault(r.category, []).append(r)
    lines.append("| category | n | section R@10 | spec R@10 | MRR |")
    lines.append("|---|---:|---:|---:|---:|")
    for cat in sorted(cats):
        subs = cats[cat]
        n = len(subs)
        secr = sum(r.metrics_by_dim.get(dim, {}).get("section_recall@10", 0.0) for r in subs) / n
        spr = sum(r.metrics_by_dim.get(dim, {}).get("spec_recall@10", 0.0) for r in subs) / n
        mrr_c = sum(r.metrics_by_dim.get(dim, {}).get("mrr", 0.0) for r in subs) / n
        lines.append(f"| {cat} | {n} | {secr:.3f} | {spr:.3f} | {mrr_c:.3f} |")
    lines.append("")
    lines.append("## Top-10 worst items (section_recall@10 = 0)")
    lines.append("")
    misses = [r for r in rows if r.metrics_by_dim.get(dim, {}).get("section_recall@10", 0.0) == 0.0]
    lines.append(f"miss count: {len(misses)} / {m.n_questions}")
    lines.append("")
    lines.append("| item_id | category | expected_specs | spec_recall@10 |")
    lines.append("|---|---|---|---:|")
    for r in misses[:10]:
        spr = r.metrics_by_dim.get(dim, {}).get("spec_recall@10", 0.0)
        specs = ",".join(r.expected_specs)
        lines.append(f"| {r.item_id} | {r.category} | {specs} | {spr:.1f} |")
    lines.append("")
    lines.append(
        f"_报告由 `eval/scripts/m6_regression.py` 自动生成（mode={mode}）；"
        f"JSON 详情见 `{json_path.name}`；口径见 `eval-results/m6-retrieval-baseline.md`。_"
    )

    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote report: %s + %s", md_path, json_path)
    return md_path


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    golden = load_golden(args.golden)
    if args.limit > 0:
        golden = golden[: args.limit]
    log.info(
        "loaded golden: %d items (limit=%d, mode=%s) from %s",
        len(golden),
        args.limit,
        args.mode,
        args.golden,
    )

    by_dim, rows = evaluate_retrieval(golden, dims=(1024,), top_k=args.top_k)

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    tag = f"eval-1024-smoke{args.limit}-{ts}" if args.limit > 0 else f"eval-1024-{ts}"
    out_dir = args.out_root / tag
    md_path = write_report(
        out_dir=out_dir,
        by_dim=by_dim,
        rows=rows,
        golden_path=args.golden,
        limit=args.limit,
        mode=args.mode,
    )

    d = by_dim[1024]
    m = d.metrics
    print(
        json.dumps(
            {
                "n": m.n_questions,
                "mode": args.mode,
                "spec_recall@10": round(m.spec_recall_at.get(10, 0), 4),
                "section_recall@10": round(m.section_recall_at.get(10, 0), 4),
                "mrr": round(m.mrr, 4),
                "mrr_spec": round(m.mrr_spec, 4),
                "latency_p50_ms": round(d.latency_ms_p50, 1),
                "report": str(md_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
