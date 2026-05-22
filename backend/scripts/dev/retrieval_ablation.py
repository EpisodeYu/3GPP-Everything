"""M7.5 retrieval ablation / 调参脚本（dev 用，非 CI 默认跑）。

口径见 `docs/04-handoff/2026-05-22-m7.5-complete.md` / `eval-results/m7-rerank-ablation.md`。

用途：

- C.2 retrieval 校准：批量跑 `eval/golden/v1.yaml`（source=hand_crafted，56 题）
  对比不同 dense top_k / RRF k / rerank top_k 组合的 spec / section recall + MRR
- C.3 rerank ablation：同子集跑 baseline (dense+BM25+RRF, top-5 fused) vs +voyage rerank-2.5

为什么写在 backend 而不是 eval/：

- 必须复用 backend `app.retrieval.{dense,sparse,hybrid,rerank}` 的真实实现 +
  生产 Settings（dim / collection / bm25_dir / RRF k 等），与生产 retrieve_node
  完全同构；放在 eval/ 会重复 30 行 + 易漂移
- BM25 by_spec 持久化目录在 host `/data/tgpp/bm25/voyage`（backend container 内
  mount 到 `/data/tgpp`），脚本默认走生产 settings，直接复用

运行（host 上，不在 container 里跑，避免内存压力）：

```
cd backend && env LITELLM_BASE_URL='http://localhost:4000/v1' \\
    QDRANT_URL='http://localhost:6333' INGEST_DATA_DIR='/data/tgpp' \\
    uv run python -m scripts.dev.retrieval_ablation \\
        --golden /home/s1yu/3GPP-Everything/eval/golden/v1.yaml \\
        --source hand_crafted \\
        --out /home/s1yu/3GPP-Everything/eval-results/m7-rerank-ablation.md
```

输出：

- stdout：每个 config 的聚合指标 + 进度
- `--out` 指定的 markdown 报告：configs 对照表 + 失败题列表
- `--json` 可选：明细 results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from app.core.config import get_settings
from app.core.errors import RetrievalError
from app.llm.litellm_client import LiteLLMClient
from app.retrieval.dense import DenseRetriever
from app.retrieval.hybrid import rrf_merge
from app.retrieval.models import RetrievedChunk
from app.retrieval.rerank import Reranker
from app.retrieval.sparse import SparseRetriever

log = logging.getLogger("retrieval_ablation")


# === golden item 加载（与 eval/runner_retrieval.py 同口径，本地拷贝避免反向依赖） ====


@dataclass(slots=True)
class GoldenItem:
    id: str
    category: str
    language: str
    question: str
    expected_specs: list[tuple[str, tuple[str, ...]]]  # (spec_id, sections)
    must_say_not_found: bool = False
    source: str = ""


def load_golden(path: Path, *, source_filter: str | None = None) -> list[GoldenItem]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    items: list[GoldenItem] = []
    for raw in doc["items"]:
        if source_filter and raw.get("source") != source_filter:
            continue
        specs: list[tuple[str, tuple[str, ...]]] = []
        for s in raw.get("expected_specs") or []:
            sid = str(s.get("spec_id") or "").strip()
            if not sid:
                continue
            specs.append((sid, tuple(str(x) for x in (s.get("sections") or []))))
        items.append(
            GoldenItem(
                id=str(raw.get("id", "")),
                category=str(raw.get("category", "")),
                language=str(raw.get("language", "en")),
                question=str(raw.get("question", "")),
                expected_specs=specs,
                must_say_not_found=bool(raw.get("must_say_not_found", False)),
                source=str(raw.get("source", "")),
            )
        )
    return items


# === metric =================================================================


def _section_segments(s: str) -> tuple[str, ...]:
    return tuple(p for p in s.strip().split(".") if p)


def is_section_prefix(expected: str, hit_section_path: list[str]) -> bool:
    exp = _section_segments(expected)
    if not exp:
        return False
    if len(exp) > len(hit_section_path):
        return False
    return all(a == b for a, b in zip(exp, hit_section_path[: len(exp)], strict=True))


def is_section_hit(expected: tuple[str, tuple[str, ...]], hit: RetrievedChunk) -> bool:
    spec_id, sections = expected
    if hit.spec_id != spec_id:
        return False
    if not sections:
        return True
    hit_path = list(hit.section_path)
    return any(is_section_prefix(sec, hit_path) for sec in sections)


def is_spec_hit(expected_specs: list[tuple[str, tuple[str, ...]]], hit: RetrievedChunk) -> bool:
    return any(hit.spec_id == sid for sid, _ in expected_specs)


def per_question_metrics(
    expected_specs: list[tuple[str, tuple[str, ...]]],
    hits: list[RetrievedChunk],
    *,
    k_list: tuple[int, ...] = (5, 10, 20),
) -> dict[str, float]:
    """单题 spec/section recall@K + MRR (section / spec)。

    `expected_specs=[]` → 负样本：返回特殊标识（main 聚合时跳过）。
    """
    out: dict[str, float] = {}
    if not expected_specs:
        # negative item：不参与召回指标聚合，标 NaN-like
        for k in k_list:
            out[f"spec_recall@{k}"] = float("nan")
            out[f"section_recall@{k}"] = float("nan")
        out["mrr"] = float("nan")
        out["mrr_spec"] = float("nan")
        return out

    mrr_section, mrr_spec = 0.0, 0.0
    for i, h in enumerate(hits, start=1):
        if mrr_section == 0.0 and any(is_section_hit(e, h) for e in expected_specs):
            mrr_section = 1.0 / i
        if mrr_spec == 0.0 and is_spec_hit(expected_specs, h):
            mrr_spec = 1.0 / i
        if mrr_section > 0 and mrr_spec > 0:
            break
    out["mrr"] = mrr_section
    out["mrr_spec"] = mrr_spec

    for k in k_list:
        sub = hits[:k]
        spec_hit = any(is_spec_hit(expected_specs, h) for h in sub)
        sec_hit = any(any(is_section_hit(e, h) for e in expected_specs) for h in sub)
        out[f"spec_recall@{k}"] = 1.0 if spec_hit else 0.0
        out[f"section_recall@{k}"] = 1.0 if sec_hit else 0.0
    return out


def _safe_mean(values: list[float]) -> float | None:
    xs = [v for v in values if v == v]  # filter NaN
    return statistics.mean(xs) if xs else None


# === 配置 ===================================================================


@dataclass(slots=True)
class AblationConfig:
    """单次 retrieval 跑的参数集。"""

    name: str
    dense_top_k: int = 30
    sparse_top_k: int = 30
    rrf_k: int = 60
    final_top_n: int = 50
    rerank_top_k: int | None = 5  # None → 不 rerank，取 fused top-rerank-top-k

    @property
    def label(self) -> str:
        rk = "no-rerank" if self.rerank_top_k is None else f"rerank{self.rerank_top_k}"
        return (
            f"d{self.dense_top_k}/s{self.sparse_top_k}/rrf{self.rrf_k}/"
            f"top{self.final_top_n}/{rk}"
        )


@dataclass(slots=True)
class PerQuestionRow:
    item_id: str
    category: str
    language: str
    expected_specs: list[str]
    expected_sections: list[str]
    config: str
    metrics: dict[str, float]
    top_hits: list[str]  # "spec_id §section_path" top-5
    timings_ms: dict[str, float]


@dataclass(slots=True)
class ConfigSummary:
    config: AblationConfig
    n_items: int  # 参与 retrieval 指标的（非 negative）题数
    n_total: int
    aggregate: dict[str, float | None]
    p50_total_ms: float
    p95_total_ms: float
    avg_stage_ms: dict[str, float]
    failed_items: list[PerQuestionRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "config": {
                "name": self.config.name,
                "label": self.config.label,
                **{
                    k: getattr(self.config, k)
                    for k in ("dense_top_k", "sparse_top_k", "rrf_k", "final_top_n", "rerank_top_k")
                },
            },
            "n_items": self.n_items,
            "n_total": self.n_total,
            "aggregate": self.aggregate,
            "p50_total_ms": round(self.p50_total_ms, 1),
            "p95_total_ms": round(self.p95_total_ms, 1),
            "avg_stage_ms": {k: round(v, 1) for k, v in self.avg_stage_ms.items()},
            "failed_items": [
                {
                    "item_id": r.item_id,
                    "category": r.category,
                    "language": r.language,
                    "expected_specs": r.expected_specs,
                    "expected_sections": r.expected_sections,
                    "section_recall@5": r.metrics.get("section_recall@5"),
                    "section_recall@10": r.metrics.get("section_recall@10"),
                    "section_recall@20": r.metrics.get("section_recall@20"),
                    "top_hits": r.top_hits,
                }
                for r in self.failed_items
            ],
        }


# === 单 config retrieval =====================================================


async def _retrieve_once(
    item: GoldenItem,
    *,
    dense: DenseRetriever,
    sparse: SparseRetriever,
    reranker: Reranker | None,
    config: AblationConfig,
) -> tuple[list[RetrievedChunk], dict[str, float]]:
    """单题：dense + sparse + rrf (+ optional rerank) → (final_hits, per-stage timings_ms)。

    遇到 RetrievalError 上游异常 → 对应 stage 计 0 hits 不挂；timings 仍记 elapsed。
    """
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    try:
        d = await dense.retrieve(item.question, top_k=config.dense_top_k)
    except RetrievalError as exc:
        log.warning("dense failed for %s: %s", item.id, exc)
        d = []
    timings["dense_ms"] = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    try:
        sp = await asyncio.to_thread(sparse.retrieve, item.question, top_k=config.sparse_top_k)
    except RetrievalError as exc:
        log.warning("sparse failed for %s: %s", item.id, exc)
        sp = []
    timings["sparse_ms"] = (time.perf_counter() - t1) * 1000.0

    t2 = time.perf_counter()
    fused = rrf_merge(d, sp, k=config.rrf_k, top_n=config.final_top_n)
    timings["rrf_ms"] = (time.perf_counter() - t2) * 1000.0

    if config.rerank_top_k is None or reranker is None:
        # 无 rerank → 取 fused top-K（K = settings.RERANK_TOP_K 默认 5；这里复用
        # final_top_n 还是另算？为了与 +rerank 公平对比，统一取前 N_for_compare）
        # 这里返回完整 fused（≤ final_top_n）；caller 按 K 截。
        timings["rerank_ms"] = 0.0
        return fused, timings

    t3 = time.perf_counter()
    try:
        reranked = await reranker.rerank(item.question, fused, top_k=config.rerank_top_k)
    except RetrievalError as exc:
        log.warning("rerank failed for %s: %s; falling back to fused", item.id, exc)
        reranked = fused[: config.rerank_top_k]
    timings["rerank_ms"] = (time.perf_counter() - t3) * 1000.0

    return reranked, timings


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round(p * (len(s) - 1))))
    return s[k]


async def run_config(
    items: list[GoldenItem],
    config: AblationConfig,
    *,
    dense: DenseRetriever,
    sparse: SparseRetriever,
    reranker: Reranker | None,
    k_list: tuple[int, ...] = (5, 10, 20),
) -> tuple[ConfigSummary, list[PerQuestionRow]]:
    rows: list[PerQuestionRow] = []
    all_timings_total: list[float] = []
    stage_timings: dict[str, list[float]] = {
        k: [] for k in ("dense_ms", "sparse_ms", "rrf_ms", "rerank_ms")
    }

    for it in items:
        hits, t = await _retrieve_once(
            it, dense=dense, sparse=sparse, reranker=reranker, config=config
        )
        total_ms = sum(t.values())
        all_timings_total.append(total_ms)
        for k in stage_timings:
            stage_timings[k].append(t.get(k, 0.0))

        m = per_question_metrics(it.expected_specs, hits, k_list=k_list)
        top_hits = [
            f"{h.spec_id} §{'.'.join(h.section_path)}" if h.section_path else h.spec_id
            for h in hits[:5]
        ]
        rows.append(
            PerQuestionRow(
                item_id=it.id,
                category=it.category,
                language=it.language,
                expected_specs=sorted({s for s, _ in it.expected_specs}),
                expected_sections=[f"{s} §{sec}" for s, secs in it.expected_specs for sec in secs],
                config=config.label,
                metrics=m,
                top_hits=top_hits,
                timings_ms={k: round(v, 1) for k, v in t.items()},
            )
        )

    # 聚合（negative item 的 NaN 已在 _safe_mean 跳过）
    valid_rows = [r for r in rows if r.expected_specs]
    agg: dict[str, float | None] = {}
    for k in k_list:
        agg[f"spec_recall@{k}"] = _safe_mean([r.metrics[f"spec_recall@{k}"] for r in valid_rows])
        agg[f"section_recall@{k}"] = _safe_mean(
            [r.metrics[f"section_recall@{k}"] for r in valid_rows]
        )
    agg["mrr"] = _safe_mean([r.metrics["mrr"] for r in valid_rows])
    agg["mrr_spec"] = _safe_mean([r.metrics["mrr_spec"] for r in valid_rows])

    # category breakdown for section_recall@5（与 D13 主硬指标对齐）
    by_cat: dict[str, list[float]] = {}
    for r in valid_rows:
        by_cat.setdefault(r.category, []).append(r.metrics["section_recall@5"])
    cat_agg = {cat: round(statistics.mean(xs), 3) for cat, xs in by_cat.items() if xs}
    agg["section_recall@5_by_category"] = cat_agg  # type: ignore[assignment]

    failed = [r for r in valid_rows if r.metrics.get(f"section_recall@{max(k_list)}", 0.0) == 0.0]

    summary = ConfigSummary(
        config=config,
        n_items=len(valid_rows),
        n_total=len(rows),
        aggregate=agg,
        p50_total_ms=_percentile(all_timings_total, 0.5),
        p95_total_ms=_percentile(all_timings_total, 0.95),
        avg_stage_ms={k: statistics.mean(v) if v else 0.0 for k, v in stage_timings.items()},
        failed_items=failed,
    )
    return summary, rows


# === Markdown 输出 ===========================================================


def _format_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _format_mrr(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}"


def write_markdown(
    summaries: list[ConfigSummary],
    *,
    out_path: Path,
    golden_path: Path,
    source_filter: str | None,
    notes: str = "",
) -> None:
    lines: list[str] = []
    lines.append("# M7.5 Retrieval Ablation 报告")
    lines.append("")
    lines.append("- 生成脚本：`backend/scripts/dev/retrieval_ablation.py`")
    lines.append(f"- golden：`{golden_path}`")
    lines.append(f"- source_filter：`{source_filter or '(all)'}`")
    lines.append(f"- n_configs：{len(summaries)}")
    if summaries:
        lines.append(
            f"- n_items（参与召回指标）：{summaries[0].n_items} / 总题数 {summaries[0].n_total}"
        )
    lines.append("")
    if notes:
        lines.append(notes)
        lines.append("")

    lines.append("## 1. 主对照表（按 section recall@5 排序）")
    lines.append("")
    lines.append(
        "| config | params | section@5 | section@10 | section@20 | spec@5 | spec@10 | MRR | p50 ms | p95 ms |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    sorted_by_main = sorted(
        summaries,
        key=lambda s: (s.aggregate.get("section_recall@5") or 0.0),
        reverse=True,
    )
    for s in sorted_by_main:
        lines.append(
            f"| **{s.config.name}** | {s.config.label} | "
            f"{_format_pct(s.aggregate.get('section_recall@5'))} | "
            f"{_format_pct(s.aggregate.get('section_recall@10'))} | "
            f"{_format_pct(s.aggregate.get('section_recall@20'))} | "
            f"{_format_pct(s.aggregate.get('spec_recall@5'))} | "
            f"{_format_pct(s.aggregate.get('spec_recall@10'))} | "
            f"{_format_mrr(s.aggregate.get('mrr'))} | "
            f"{s.p50_total_ms:.0f} | {s.p95_total_ms:.0f} |"
        )
    lines.append("")

    lines.append("## 2. 按 category 拆 section_recall@5")
    lines.append("")
    if summaries:
        all_cats = sorted(
            {
                cat for s in summaries for cat in (s.aggregate.get("section_recall@5_by_category") or {})  # type: ignore[union-attr]
            }
        )
        header = "| config | " + " | ".join(all_cats) + " |"
        sep = "|---|" + "|".join(["---:"] * len(all_cats)) + "|"
        lines.append(header)
        lines.append(sep)
        for s in summaries:
            cat_agg = s.aggregate.get("section_recall@5_by_category") or {}
            row = "| " + s.config.name + " | "
            row += " | ".join(
                _format_pct(cat_agg.get(c)) if c in cat_agg else "—" for c in all_cats  # type: ignore[attr-defined]
            )
            row += " |"
            lines.append(row)
    lines.append("")

    lines.append("## 3. 平均 per-stage latency")
    lines.append("")
    lines.append("| config | dense ms | sparse ms | rrf ms | rerank ms | total p50 ms |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for s in summaries:
        st = s.avg_stage_ms
        lines.append(
            f"| {s.config.name} | "
            f"{st.get('dense_ms', 0):.0f} | {st.get('sparse_ms', 0):.0f} | "
            f"{st.get('rrf_ms', 0):.0f} | {st.get('rerank_ms', 0):.0f} | "
            f"{s.p50_total_ms:.0f} |"
        )
    lines.append("")

    lines.append("## 4. 每 config 失败（section_recall@20 = 0）题清单")
    lines.append("")
    for s in summaries:
        lines.append(f"### {s.config.name} — {len(s.failed_items)} fail")
        lines.append("")
        if not s.failed_items:
            lines.append("（无）")
            lines.append("")
            continue
        for r in s.failed_items:
            exp = "; ".join(r.expected_sections) or "; ".join(r.expected_specs)
            top = " / ".join(r.top_hits[:3])
            lines.append(f"- **{r.item_id}** ({r.category}/{r.language})")
            lines.append(f"  - expected: {exp}")
            lines.append(f"  - top-3 hits: {top}")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote markdown report: %s", out_path)


# === main ===================================================================


_PRESET_CONFIGS: dict[str, AblationConfig] = {
    "baseline_no_rerank": AblationConfig(
        name="baseline_no_rerank",
        dense_top_k=30,
        sparse_top_k=30,
        rrf_k=60,
        final_top_n=50,
        rerank_top_k=None,
    ),
    "baseline_rerank5": AblationConfig(
        name="baseline_rerank5",
        dense_top_k=30,
        sparse_top_k=30,
        rrf_k=60,
        final_top_n=50,
        rerank_top_k=5,
    ),
    "baseline_rerank10": AblationConfig(
        name="baseline_rerank10",
        dense_top_k=30,
        sparse_top_k=30,
        rrf_k=60,
        final_top_n=50,
        rerank_top_k=10,
    ),
    "wide_dense_rerank5": AblationConfig(
        name="wide_dense_rerank5",
        dense_top_k=50,
        sparse_top_k=50,
        rrf_k=60,
        final_top_n=80,
        rerank_top_k=5,
    ),
    "wide_dense_rerank10": AblationConfig(
        name="wide_dense_rerank10",
        dense_top_k=50,
        sparse_top_k=50,
        rrf_k=60,
        final_top_n=80,
        rerank_top_k=10,
    ),
    "narrow_rrf_rerank5": AblationConfig(
        name="narrow_rrf_rerank5",
        dense_top_k=30,
        sparse_top_k=30,
        rrf_k=30,
        final_top_n=50,
        rerank_top_k=5,
    ),
    "wide_rrf_rerank5": AblationConfig(
        name="wide_rrf_rerank5",
        dense_top_k=30,
        sparse_top_k=30,
        rrf_k=100,
        final_top_n=50,
        rerank_top_k=5,
    ),
}


async def main_async(args: argparse.Namespace) -> int:
    # 把 logging 输出导到 stdout（默认走 stderr）；这样调用方可以 `2>/dev/null`
    # 屏蔽 bm25s 的 tqdm 进度条噪声而不丢 logging。
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    items = load_golden(Path(args.golden), source_filter=args.source)
    log.info("loaded %d items (source_filter=%s)", len(items), args.source)
    if args.subset:
        items = items[: args.subset]
        log.info("subset to first %d items", len(items))

    if not items:
        log.error("no items to evaluate")
        return 1

    configs: list[AblationConfig] = []
    if args.configs:
        for name in args.configs:
            if name not in _PRESET_CONFIGS:
                log.error("unknown config name: %s; available: %s", name, sorted(_PRESET_CONFIGS))
                return 2
            configs.append(_PRESET_CONFIGS[name])
    else:
        configs = list(_PRESET_CONFIGS.values())

    s = get_settings()
    log.info(
        "settings dump: dim=%d collection=%s bm25=%s litellm=%s qdrant=%s",
        s.EMBEDDING_DIMENSIONS,
        s.qdrant_collection,
        s.bm25_dir,
        s.LITELLM_BASE_URL,
        s.QDRANT_URL,
    )

    t0 = time.perf_counter()
    sparse = SparseRetriever.from_env(settings=s)
    log.info("bm25 loaded: docs=%d in %.1fs", sparse.n, time.perf_counter() - t0)

    cli = LiteLLMClient(settings=s)
    dense = DenseRetriever.from_env(embedder=cli, settings=s)
    reranker = Reranker.from_env(litellm_client=cli, settings=s)

    summaries: list[ConfigSummary] = []
    all_rows: list[PerQuestionRow] = []
    try:
        for cfg in configs:
            log.info("===== running config: %s (%s) =====", cfg.name, cfg.label)
            t = time.perf_counter()
            summary, rows = await run_config(
                items,
                cfg,
                dense=dense,
                sparse=sparse,
                reranker=reranker,
            )
            all_rows.extend(rows)
            summaries.append(summary)
            log.info(
                "[%s] n=%d/%d  section@5=%s  section@10=%s  spec@5=%s  MRR=%s  p50=%.0fms  (elapsed=%.1fs)",
                cfg.name,
                summary.n_items,
                summary.n_total,
                _format_pct(summary.aggregate.get("section_recall@5")),
                _format_pct(summary.aggregate.get("section_recall@10")),
                _format_pct(summary.aggregate.get("spec_recall@5")),
                _format_mrr(summary.aggregate.get("mrr")),
                summary.p50_total_ms,
                time.perf_counter() - t,
            )
    finally:
        await dense.close()
        await cli.close()

    out_path = Path(args.out)
    write_markdown(
        summaries,
        out_path=out_path,
        golden_path=Path(args.golden),
        source_filter=args.source,
        notes=args.notes or "",
    )
    log.info("report → %s", out_path)

    if args.json:
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(
                {
                    "summaries": [s.to_dict() for s in summaries],
                    "rows": [
                        {
                            **asdict(r),
                            "metrics": {k: (None if v != v else v) for k, v in r.metrics.items()},
                        }
                        for r in all_rows
                    ],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        log.info("json detail → %s", json_path)

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="M7.5 retrieval ablation runner")
    p.add_argument("--golden", required=True, help="eval/golden/v1.yaml 路径")
    p.add_argument("--source", default=None, help="过滤 source（默认 None = all）")
    p.add_argument("--subset", type=int, default=0, help="只跑前 N 题（debug 用）")
    p.add_argument(
        "--configs",
        nargs="*",
        default=None,
        help=f"配置名子集；默认全跑。可选：{sorted(_PRESET_CONFIGS)}",
    )
    p.add_argument("--out", required=True, help="markdown 报告输出路径")
    p.add_argument("--json", default=None, help="可选：明细 results.json 输出路径")
    p.add_argument("--notes", default=None, help="顶部 notes 段落（markdown）")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
