"""T4+T5 retrieval-only 评测 runner（维度决胜核心）。

输入：eval/golden/v1.yaml（schema: docs/03-development/06-...md §3.5）
流程：
    每题 → embed(question, multidim=dims) → 各 dim qdrant.search → per_question_metrics
    → 聚合 RetrievalMetrics by dim → 报告 markdown + json + 决胜 verdict

决胜规则（D6, docs/03-development/06-...md §8）：
- R@10 差距 > 2% → 选 R@10 高者
- 否则比 MRR；MRR 差距 > 2% → 选高者
- 否则差距不显著 → 选 1024 维（存储 / latency 优势）

输出：
    eval-results/m3-embedding-poc/{ts}/results.json
    eval-results/m3-embedding-poc/{ts}/report.md
    eval-results/m3-embedding-poc.md（最终决胜记录；人签字位）
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from eval.retrieval.metrics import (
    ExpectedSpec,
    HitRef,
    RetrievalMetrics,
    compute_metrics,
    per_question_metrics,
)
from eval.retrieval.retriever import Retriever

log = logging.getLogger(__name__)


@dataclass(slots=True)
class GoldenItem:
    """golden v1.yaml 中单题的 dataclass 视图。"""

    id: str
    category: str
    language: str
    question: str
    expected_specs: list[ExpectedSpec]
    expected_facts: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    must_say_not_found: bool = False
    source: str = ""
    teleqna_origin_id: str | None = None
    notes: str = ""


def load_golden(path: Path) -> list[GoldenItem]:
    """读 v1.yaml → list[GoldenItem]。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "items" not in doc:
        raise ValueError(f"invalid golden YAML at {path}: missing 'items' key")
    items: list[GoldenItem] = []
    for raw in doc["items"]:
        specs: list[ExpectedSpec] = []
        for s in raw.get("expected_specs") or []:
            spec_id = str(s.get("spec_id", "")).strip()
            if not spec_id:
                continue
            sections = tuple(str(x) for x in (s.get("sections") or []))
            specs.append(ExpectedSpec(spec_id=spec_id, sections=sections))
        items.append(
            GoldenItem(
                id=str(raw.get("id", "")),
                category=str(raw.get("category", "")),
                language=str(raw.get("language", "en")),
                question=str(raw.get("question", "")),
                expected_specs=specs,
                expected_facts=[str(f) for f in raw.get("expected_facts") or []],
                forbidden=[str(f) for f in raw.get("forbidden") or []],
                must_say_not_found=bool(raw.get("must_say_not_found", False)),
                source=str(raw.get("source", "")),
                teleqna_origin_id=(
                    str(raw["teleqna_origin_id"])
                    if raw.get("teleqna_origin_id") is not None
                    else None
                ),
                notes=str(raw.get("notes", "")),
            )
        )
    return items


@dataclass(slots=True)
class PerQuestionRow:
    """一条 golden item 在所有 dim 上的指标。"""

    item_id: str
    category: str
    expected_specs: list[str]
    metrics_by_dim: dict[int, dict[str, float]]
    latency_ms_by_dim: dict[int, float]


@dataclass(slots=True)
class DimResult:
    """单一 dim 的聚合 metrics + 性能。"""

    dim: int
    metrics: RetrievalMetrics
    latency_ms_p50: float
    latency_ms_p95: float
    n_questions: int

    def to_dict(self) -> dict:
        return {
            "dim": self.dim,
            "n_questions": self.n_questions,
            "metrics": self.metrics.to_dict(),
            "latency_ms_p50": round(self.latency_ms_p50, 1),
            "latency_ms_p95": round(self.latency_ms_p95, 1),
        }


@dataclass(slots=True)
class DecisionVerdict:
    """决胜结论。"""

    winner_dim: int
    reason: str
    r10_diff: float
    mrr_diff: float
    tie_fallback: bool

    def to_dict(self) -> dict:
        return {
            "winner_dim": self.winner_dim,
            "reason": self.reason,
            "r10_diff_pp": round(self.r10_diff * 100, 2),
            "mrr_diff_pp": round(self.mrr_diff * 100, 2),
            "tie_fallback_to_1024": self.tie_fallback,
        }


# 决胜规则（docs/03-development/06-evaluation-and-observability.md §8）
DECISION_THRESHOLD = 0.02  # 2%


def decide_winner(
    by_dim: dict[int, DimResult],
    *,
    threshold: float = DECISION_THRESHOLD,
    tie_fallback_dim: int = 1024,
) -> DecisionVerdict:
    """按 R@10 / MRR 差距判赢家；都不显著回退到 1024。"""
    if set(by_dim.keys()) != {2048, 1024}:
        raise ValueError(
            f"decide_winner: expected dims={{2048, 1024}}, got {sorted(by_dim.keys())}"
        )
    r2048 = by_dim[2048].metrics.section_recall_at.get(10, 0.0)
    r1024 = by_dim[1024].metrics.section_recall_at.get(10, 0.0)
    mrr2048 = by_dim[2048].metrics.mrr
    mrr1024 = by_dim[1024].metrics.mrr

    r_diff = r2048 - r1024
    mrr_diff = mrr2048 - mrr1024

    if abs(r_diff) > threshold:
        winner = 2048 if r_diff > 0 else 1024
        return DecisionVerdict(
            winner_dim=winner,
            reason=f"R@10 差距 {abs(r_diff)*100:.2f}pp > {threshold*100:.0f}pp → 选 R@10 高者",
            r10_diff=r_diff,
            mrr_diff=mrr_diff,
            tie_fallback=False,
        )
    if abs(mrr_diff) > threshold:
        winner = 2048 if mrr_diff > 0 else 1024
        return DecisionVerdict(
            winner_dim=winner,
            reason=(
                f"R@10 差距 ≤ {threshold*100:.0f}pp；MRR 差距 {abs(mrr_diff)*100:.2f}pp "
                f"> {threshold*100:.0f}pp → 选 MRR 高者"
            ),
            r10_diff=r_diff,
            mrr_diff=mrr_diff,
            tie_fallback=False,
        )
    return DecisionVerdict(
        winner_dim=tie_fallback_dim,
        reason=(
            f"R@10 差距 {abs(r_diff)*100:.2f}pp 与 MRR 差距 {abs(mrr_diff)*100:.2f}pp "
            f"均 ≤ {threshold*100:.0f}pp → 回退选 {tie_fallback_dim} 维（存储 / latency 优势）"
        ),
        r10_diff=r_diff,
        mrr_diff=mrr_diff,
        tie_fallback=True,
    )


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round(p * (len(s) - 1))))
    return s[k]


def evaluate_retrieval(
    golden: list[GoldenItem],
    *,
    dims: tuple[int, ...] = (2048, 1024),
    top_k: int = 20,
    k_list: tuple[int, ...] = (5, 10, 20),
    retriever: Retriever | None = None,
) -> tuple[dict[int, DimResult], list[PerQuestionRow]]:
    """端到端 retrieval-only 评测；retriever=None 时按 .env 自建。"""
    owned_retriever = retriever is None
    r = retriever or Retriever()
    rows: list[PerQuestionRow] = []
    per_dim_metrics_rows: dict[int, list[dict[str, float]]] = {d: [] for d in dims}
    per_dim_latencies: dict[int, list[float]] = {d: [] for d in dims}

    try:
        for it in golden:
            if not it.question:
                log.warning("skip empty question id=%s", it.id)
                continue
            t0 = time.perf_counter()
            hits_by_dim = r.search_multidim(it.question, dims=dims, top_k=top_k)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            metrics_by_dim: dict[int, dict[str, float]] = {}
            latency_by_dim: dict[int, float] = {}
            for dim in dims:
                hits = hits_by_dim.get(dim, [])
                hit_refs = [HitRef.from_hit(h) for h in hits]
                m = per_question_metrics(it.expected_specs, hit_refs, k_list=k_list)
                metrics_by_dim[dim] = m
                per_dim_metrics_rows[dim].append(m)
                # 简化：把 multi-dim 的总 latency 按 dim 数平均（embed 一次 + N 次 qdrant query）
                latency_by_dim[dim] = elapsed_ms / max(len(dims), 1)
                per_dim_latencies[dim].append(latency_by_dim[dim])

            rows.append(
                PerQuestionRow(
                    item_id=it.id,
                    category=it.category,
                    expected_specs=[e.spec_id for e in it.expected_specs],
                    metrics_by_dim=metrics_by_dim,
                    latency_ms_by_dim=latency_by_dim,
                )
            )
    finally:
        if owned_retriever:
            r.close()

    by_dim: dict[int, DimResult] = {}
    for dim in dims:
        agg = compute_metrics(per_dim_metrics_rows[dim], k_list=k_list)
        by_dim[dim] = DimResult(
            dim=dim,
            metrics=agg,
            latency_ms_p50=_percentile(per_dim_latencies[dim], 0.5),
            latency_ms_p95=_percentile(per_dim_latencies[dim], 0.95),
            n_questions=agg.n_questions,
        )
    return by_dim, rows


def write_report_markdown(
    *,
    out_dir: Path,
    by_dim: dict[int, DimResult],
    verdict: DecisionVerdict,
    rows: list[PerQuestionRow],
    golden_path: Path,
    provider: str = "voyage",
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "report.md"
    json_path = out_dir / "results.json"

    json_path.write_text(
        json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "golden_path": str(golden_path),
                "provider": provider,
                "by_dim": {str(d): r.to_dict() for d, r in by_dim.items()},
                "verdict": verdict.to_dict(),
                "n_questions": next(iter(by_dim.values())).n_questions if by_dim else 0,
                "per_question": [
                    {
                        "item_id": r.item_id,
                        "category": r.category,
                        "expected_specs": r.expected_specs,
                        "metrics": {str(d): m for d, m in r.metrics_by_dim.items()},
                        "latency_ms": {str(d): v for d, v in r.latency_ms_by_dim.items()},
                    }
                    for r in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # markdown 报告
    lines: list[str] = []
    lines.append(f"# M3 维度决胜评测报告（{datetime.now(UTC).strftime('%Y-%m-%d')}）")
    lines.append("")
    lines.append(f"- golden: `{golden_path}`")
    lines.append(f"- provider: `{provider}`")
    lines.append(f"- n_questions: {next(iter(by_dim.values())).n_questions if by_dim else 0}")
    lines.append("")
    lines.append("## 决胜结论")
    lines.append("")
    lines.append(f"**winner = {verdict.winner_dim}**")
    lines.append("")
    lines.append(f"- 原因：{verdict.reason}")
    lines.append(f"- R@10 差距：{verdict.r10_diff*100:+.2f}pp（2048 − 1024）")
    lines.append(f"- MRR 差距：{verdict.mrr_diff*100:+.2f}pp（2048 − 1024）")
    if verdict.tie_fallback:
        lines.append("- 触发 tie-fallback → 选 1024 维（存储省一半 + latency 优势）")
    lines.append("")
    lines.append("## 聚合指标")
    lines.append("")
    lines.append(
        "| dim | n | R@5 | R@10 | R@20 | spec R@10 | MRR | P@10 | latency p50 (ms) | latency p95 (ms) |"
    )
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for dim in sorted(by_dim.keys(), reverse=True):
        d = by_dim[dim]
        m = d.metrics
        lines.append(
            f"| {dim} | {d.n_questions} | "
            f"{m.section_recall_at.get(5, 0):.3f} | "
            f"{m.section_recall_at.get(10, 0):.3f} | "
            f"{m.section_recall_at.get(20, 0):.3f} | "
            f"{m.spec_recall_at.get(10, 0):.3f} | "
            f"{m.mrr:.3f} | "
            f"{m.precision_at.get(10, 0):.3f} | "
            f"{d.latency_ms_p50:.1f} | "
            f"{d.latency_ms_p95:.1f} |"
        )
    lines.append("")
    # category breakdown
    lines.append("## 按 category 分布（section_recall@10）")
    lines.append("")
    cats: dict[str, list[PerQuestionRow]] = {}
    for r in rows:
        cats.setdefault(r.category, []).append(r)
    lines.append("| category | n | R@10 (2048) | R@10 (1024) |")
    lines.append("|---|---:|---:|---:|")
    for cat in sorted(cats):
        subs = cats[cat]
        n = len(subs)
        r2048 = sum(r.metrics_by_dim.get(2048, {}).get("section_recall@10", 0.0) for r in subs) / n
        r1024 = sum(r.metrics_by_dim.get(1024, {}).get("section_recall@10", 0.0) for r in subs) / n
        lines.append(f"| {cat} | {n} | {r2048:.3f} | {r1024:.3f} |")
    lines.append("")
    lines.append("## 人审签字位")
    lines.append("")
    lines.append("- [ ] 数据复核（决胜规则按 D6 沿用，结论与表格一致）")
    lines.append("- [ ] 决议：选用 `_d{winner}` collection，drop 输者 collection 节省 ~2.5-5GB")
    lines.append(
        f"- [ ] 后续：Qdrant `tgpp_chunks_{provider}_d{verdict.winner_dim}` 进入 M6 全量索引"
    )
    lines.append("")
    lines.append(f"_报告由 `eval/runner_retrieval.py` 自动生成；JSON 详情见 `{json_path.name}`。_")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote report: %s + %s", md_path, json_path)
    return md_path


__all__ = [
    "DECISION_THRESHOLD",
    "DecisionVerdict",
    "DimResult",
    "GoldenItem",
    "PerQuestionRow",
    "decide_winner",
    "evaluate_retrieval",
    "load_golden",
    "write_report_markdown",
]
