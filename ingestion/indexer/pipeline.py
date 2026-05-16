"""单 spec / 多 spec 端到端 indexer 编排（docs §4.4 / §4.7）。

工作流（单 spec）：

```
SpecBundle → chunker.build_chunks → vision_resolver（可选）
          ↓
       List[Chunk]
          ↓
       embedder.embed_texts (batch 64)
          ↓
   ┌──────┴──────┬──────────────┐
   ↓             ↓              ↓
QdrantWriter   BM25Writer    PgChunkMetaWriter
.upsert_chunks .write_spec_chunks   .upsert_chunks
```

设计要点：

- **失败语义**：indexer 内任一步抛异常 → 整 spec 视为失败；上层 pipeline-hf 捕获后
  continue 下一篇，不会因为一个 spec 炸毁全量任务
- **续传**：依赖 Qdrant upsert + PG (chunk_id, provider) UNIQUE 的幂等性；
  重跑同 spec 不会出现 duplicate。`--skip-indexed` 选项查 Qdrant point 数判断
- **多 provider**：每次跑只对应一个 provider；要同时跑 voyage + glm，调两次 `index_spec`
- **vision_resolver**：可选注入；不传时 figure chunk 用 GSMA 自带描述（fallback）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ingestion.chunker import ChunkParams, build_chunks
from ingestion.chunker.figure import VisionResolver as ChunkerVisionResolver
from ingestion.chunker.models import Chunk
from ingestion.hf_loader.models import SpecBundle
from ingestion.rate_limit import get_mimo_limiter, get_voyage_limiter

from .bm25_writer import BM25Writer
from .embedder import DEFAULT_MULTIDIM_DIMS, Embedder, embed_chunks
from .models import IndexStats, PipelineStats, Provider
from .pg_writer import PgChunkMetaWriter
from .qdrant_writer import QdrantWriter

# 不在 import 时调 get_voyage_limiter：测试有 reset_singletons / fake 注入路径，
# 真正用时再取，避免提前 freeze 单例。

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IndexerComponents:
    """单 spec indexer 所需的全部"接线"。

    便捷场景：pipeline-hf 一次性构造一组 components，跨 spec 复用（embedder /
    qdrant client / pg engine 都不要每个 spec 都重建）。
    """

    embedder: Embedder
    qdrant: QdrantWriter
    bm25: BM25Writer
    pg: PgChunkMetaWriter | None  # None 时跳过 PG 写入（CI / 无 PG 环境）
    vision_resolver: ChunkerVisionResolver | None = None

    def close(self) -> None:
        for c in (self.embedder, self.qdrant, self.pg):
            if c is None:
                continue
            with contextlib.suppress(Exception):  # pragma: no cover - 清理路径
                c.close()

    def __enter__(self) -> IndexerComponents:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def index_spec(
    bundle: SpecBundle,
    components: IndexerComponents,
    *,
    chunk_params: ChunkParams | None = None,
    purge_before: bool = True,
) -> IndexStats:
    """单 spec 端到端 indexer。

    参数：
    - purge_before: 写之前先按 spec_id 删 Qdrant / PG / BM25 by_spec/，
      避免 chunk_id 变化（如 vision 改了 content）后旧 chunk 残留。
      默认 True（plan §3 选定的强幂等语义）。
    """
    t0 = time.time()
    stats = IndexStats(spec_id=bundle.spec_id)
    try:
        # 1) chunker
        raw_chunks, build_stats = build_chunks(
            bundle,
            params=chunk_params or ChunkParams(),
            vision_resolver=components.vision_resolver,
        )
        # 兜底：去重同 chunk_id（chunker 在极少数 section 内会产生 content 完全
        # 相同的副本 chunk → 同 uuid5 ID；PG `UNIQUE(chunk_id, provider)` 会拒。
        # Qdrant / BM25 也只需要 unique。这是 chunker P3 issue（已记录），
        # indexer 在入口去重，不影响检索质量）。
        chunks = _dedupe_chunks(raw_chunks)
        if len(chunks) < len(raw_chunks):
            log.warning(
                "spec %s: deduplicated chunks %d → %d (chunker produced %d duplicates)",
                bundle.spec_id,
                len(raw_chunks),
                len(chunks),
                len(raw_chunks) - len(chunks),
            )
        stats.chunks_total = len(chunks)
        stats.chunks_by_type = {
            k: sum(1 for c in chunks if c.chunk_type == k) for k in dict(build_stats.chunks_by_type)
        }
        if not chunks:
            log.warning("spec %s produced 0 chunks; skipping index", bundle.spec_id)
            return stats

        # 2) embedder（warmup 顺带探测 dim）
        embedding = embed_chunks(components.embedder, chunks)
        stats.vectors_dim = embedding.dim
        stats.embedding_tokens = embedding.prompt_tokens
        stats.embedding_calls = max(
            1, (len(chunks) + components.embedder.batch_size - 1) // components.embedder.batch_size
        )

        # 3) qdrant ensure collection（首次或 dim 探测后）
        components.qdrant.ensure_collection(dim=embedding.dim)

        # 4) purge spec (Qdrant + BM25 + PG)
        if purge_before:
            removed_q = components.qdrant.purge_spec(bundle.spec_id)
            components.bm25.purge_spec(bundle.spec_id)
            if components.pg is not None:
                removed_pg = components.pg.purge_spec(bundle.spec_id)
                log.info(
                    "purge_before: spec=%s qdrant=%d pg=%d",
                    bundle.spec_id,
                    removed_q,
                    removed_pg,
                )

        # 5) Qdrant upsert
        stats.qdrant_upserted = components.qdrant.upsert_chunks(chunks, embedding.vectors)

        # 6) BM25 持久化
        stats.bm25_persisted = components.bm25.write_spec_chunks(bundle.spec_id, chunks)

        # 7) PG chunks_meta upsert
        if components.pg is not None:
            stats.pg_upserted = components.pg.upsert_chunks(chunks)
        stats.chunks_indexed = stats.qdrant_upserted
    except Exception as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
        log.exception("index_spec failed: spec=%s", bundle.spec_id)
    finally:
        stats.elapsed_s = round(time.time() - t0, 2)
        _record_voyage_usage(stats)
    return stats


def index_specs(
    bundles: Iterable[SpecBundle],
    components: IndexerComponents,
    *,
    chunk_params: ChunkParams | None = None,
    purge_before: bool = True,
    skip_indexed: bool = False,
    progress_cb: Callable[[IndexStats], None] | None = None,
    finalize_bm25: bool = True,
) -> PipelineStats:
    """跨 spec 编排：依次跑 index_spec，单 spec 失败 continue。

    参数：
    - skip_indexed: 若 True，先查 Qdrant 中该 spec_id 点数 > 0 则跳过（续传用）；
      新内容 push 必须用 False 让 purge_before 生效
    - finalize_bm25: pipeline 跑完后是否合并 BM25 chunks.jsonl + meta.json

    返回 PipelineStats（含每篇 spec 的 stats），CLI 据此输出报告。
    """
    t0 = time.time()
    pstats = PipelineStats(provider=components.embedder.provider)
    for bundle in bundles:
        pstats.specs_attempted += 1
        if skip_indexed and components.qdrant.count(spec_id=bundle.spec_id) > 0:
            log.info("skip_indexed: %s already has points in qdrant", bundle.spec_id)
            pstats.specs_succeeded += 1
            continue
        stats = index_spec(
            bundle,
            components,
            chunk_params=chunk_params,
            purge_before=purge_before,
        )
        if stats.succeeded:
            pstats.specs_succeeded += 1
            pstats.chunks_total += stats.chunks_total
            pstats.qdrant_upserted += stats.qdrant_upserted
            pstats.embedding_tokens += stats.embedding_tokens
        else:
            pstats.specs_failed += 1
            pstats.failures.append((bundle.spec_id, stats.error or "(no message)"))
        if progress_cb is not None:
            try:
                progress_cb(stats)
            except Exception:  # pragma: no cover - 监控回调不应炸主流程
                log.exception("progress_cb failed")

    if finalize_bm25:
        try:
            components.bm25.finalize()
        except Exception:
            log.exception("bm25 finalize failed (continuing)")

    # 与 pipeline_concurrent 对齐：sync 路径也回填 voyage / mimo limiter snapshot，
    # 让 PipelineStats.voyage_tokens_total / requests_total 反映真实 sync embed 速率
    # （sync embed 不走 async limiter.with_rate_limit；index_spec 完成时 _record_voyage_usage
    # 已把单 spec 用量累加到 limiter.usage 上，这里 snapshot 取累计值）。
    _snapshot_limiters(pstats)

    pstats.elapsed_s = round(time.time() - t0, 2)
    return pstats


def index_spec_multidim(
    bundle: SpecBundle,
    components: IndexerComponents,
    *,
    dims: Sequence[int] = DEFAULT_MULTIDIM_DIMS,
    chunk_params: ChunkParams | None = None,
    purge_before: bool = True,
) -> IndexStats:
    """单 spec 端到端 indexer（multidim 版，M2 §4.7）。

    与 `index_spec` 区别：
      - embedding 走 `embed_texts_multidim`（一次 API 调用 + truncate+L2 派生）
      - Qdrant 写多个 `_d{dim}` collection（共享同一 chunk_id）
      - `qdrant_upserted` 取最大 dim 的 upsert 计数（兼容字段）；细分见 `qdrant_upserted_by_dim`
      - BM25 / PG 仍写一份（与 dim 无关）
    """
    t0 = time.time()
    stats = IndexStats(spec_id=bundle.spec_id)
    try:
        raw_chunks, build_stats = build_chunks(
            bundle,
            params=chunk_params or ChunkParams(),
            vision_resolver=components.vision_resolver,
        )
        chunks = _dedupe_chunks(raw_chunks)
        if len(chunks) < len(raw_chunks):
            log.warning(
                "spec %s: deduplicated chunks %d → %d",
                bundle.spec_id,
                len(raw_chunks),
                len(chunks),
            )
        stats.chunks_total = len(chunks)
        stats.chunks_by_type = {
            k: sum(1 for c in chunks if c.chunk_type == k) for k in dict(build_stats.chunks_by_type)
        }
        if not chunks:
            log.warning("spec %s produced 0 chunks; skipping index", bundle.spec_id)
            return stats

        # 多档 embedding（一次 API 调用 + 派生）
        multi = components.embedder.embed_texts_multidim([c.content for c in chunks], dims=dims)
        stats.vectors_dim = multi.dim_main
        stats.embedding_tokens = multi.prompt_tokens
        stats.embedding_calls = max(
            1,
            (len(chunks) + components.embedder.batch_size - 1) // components.embedder.batch_size,
        )

        components.qdrant.ensure_collections(list(multi.vectors_by_dim.keys()))

        if purge_before:
            removed_q = components.qdrant.purge_spec_multidim(bundle.spec_id)
            components.bm25.purge_spec(bundle.spec_id)
            removed_pg = 0
            if components.pg is not None:
                removed_pg = components.pg.purge_spec(bundle.spec_id)
            log.info(
                "purge_before(multidim): spec=%s qdrant=%s pg=%d",
                bundle.spec_id,
                removed_q,
                removed_pg,
            )

        per_dim = components.qdrant.upsert_multidim(chunks, multi.vectors_by_dim)
        stats.qdrant_upserted_by_dim = dict(per_dim)
        stats.qdrant_upserted = per_dim.get(multi.dim_main, 0)

        stats.bm25_persisted = components.bm25.write_spec_chunks(bundle.spec_id, chunks)
        if components.pg is not None:
            stats.pg_upserted = components.pg.upsert_chunks(chunks)
        stats.chunks_indexed = stats.qdrant_upserted
    except Exception as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
        log.exception("index_spec_multidim failed: spec=%s", bundle.spec_id)
    finally:
        stats.elapsed_s = round(time.time() - t0, 2)
        _record_voyage_usage(stats)
    return stats


async def _prefetch_vision_for_bundle(
    bundle: SpecBundle,
    vision: ChunkerVisionResolver,
    *,
    concurrent: int,
) -> int:
    """对 bundle 内所有 figure 异步预热 vision 缓存（M2 §4.8 fan-out）。

    - 调用 `vision.aresolve_batch`，结果写进 Redis；
    - 后续 chunker 同步走 `vision.__call__` 时直接命中缓存
    - 返回触发的图片数（含已缓存命中数）
    """
    paths = list(getattr(bundle.entry, "image_paths", ()) or ())
    if not paths:
        return 0
    if not hasattr(vision, "aresolve_batch"):
        return 0
    minimal_ctx = {
        "spec_id": bundle.spec_id,
        "clause": "",
        "section_title": "",
    }
    items = [(p, minimal_ctx) for p in paths]
    await vision.aresolve_batch(items, concurrent=concurrent)
    return len(paths)


async def pipeline_concurrent(
    bundles: Iterable[SpecBundle],
    components: IndexerComponents,
    *,
    workers: int = 3,
    vision_concurrent: int = 8,
    dims: Sequence[int] = DEFAULT_MULTIDIM_DIMS,
    chunk_params: ChunkParams | None = None,
    purge_before: bool = True,
    finalize_bm25: bool = True,
    skip_indexed: bool = False,
    progress_cb: Callable[[IndexStats], None] | None = None,
    dead_letter_dir: str | Path | None = None,
) -> PipelineStats:
    """并行 multidim pipeline（M2 §4.8 + B4 落地）。

    流程（每 worker）：
      load → vision prefetch (async fan-out) → chunker（vision 命中缓存）
      → embed multidim → upsert multi-collection + bm25 + pg

    并发模型：
      - `asyncio.Queue` 分发 spec
      - N=workers 个 async worker 任务
      - vision 受 mimo `CompositeLimiter` 全局限速；voyage 当前 sync 不限速
      - 每 spec 走 `asyncio.to_thread` 跑 sync indexer，主 loop 仅做 IO + dispatch

    失败：
      - 单 spec 任何异常 → IndexStats.error；不阻塞其他 spec
      - 失败 spec 写 `dead_letter_dir/{ts}_{spec_id}.json`（默认 INGEST_DATA_DIR/failed/）

    返回 `PipelineStats`：含 mimo / voyage 限速器快照。
    """
    workers = max(1, int(workers))
    bundle_list = list(bundles)
    pstats = PipelineStats(provider=components.embedder.provider)
    t0 = time.time()

    # 一次性 ensure 所有 dim collection（idempotent；worker 内复用）
    if bundle_list:
        components.qdrant.ensure_collections(list(dims))

    dl_dir = _resolve_dead_letter_dir(dead_letter_dir)

    queue: asyncio.Queue[SpecBundle] = asyncio.Queue()
    for b in bundle_list:
        queue.put_nowait(b)

    stats_lock = asyncio.Lock()

    async def _record(stats: IndexStats, bundle: SpecBundle) -> None:
        async with stats_lock:
            pstats.specs_attempted += 1
            if stats.succeeded:
                pstats.specs_succeeded += 1
                pstats.chunks_total += stats.chunks_total
                pstats.qdrant_upserted += stats.qdrant_upserted
                pstats.embedding_tokens += stats.embedding_tokens
                for d, n in stats.qdrant_upserted_by_dim.items():
                    pstats.qdrant_upserted_by_dim[d] = pstats.qdrant_upserted_by_dim.get(d, 0) + n
            else:
                pstats.specs_failed += 1
                pstats.failures.append((bundle.spec_id, stats.error or "(no message)"))
                _write_dead_letter(dl_dir, bundle.spec_id, stats)
        if progress_cb is not None:
            try:
                progress_cb(stats)
            except Exception:  # pragma: no cover
                log.exception("progress_cb failed")

    async def _worker(worker_id: int) -> None:
        while True:
            try:
                bundle = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if skip_indexed and components.qdrant.count(spec_id=bundle.spec_id) > 0:
                    log.info("skip_indexed[%d]: %s already indexed", worker_id, bundle.spec_id)
                    skip_stats = IndexStats(spec_id=bundle.spec_id)
                    await _record(skip_stats, bundle)
                    continue
                # 1) 异步预热 vision 缓存
                if components.vision_resolver is not None:
                    try:
                        await _prefetch_vision_for_bundle(
                            bundle, components.vision_resolver, concurrent=vision_concurrent
                        )
                    except Exception:  # vision prefetch 失败不阻塞 spec（chunker 内仍会兜底）
                        log.exception(
                            "vision prefetch failed for %s; continuing",
                            bundle.spec_id,
                        )
                # 2) 同步 indexer 跑在 thread，主 loop 不阻塞
                stats = await asyncio.to_thread(
                    index_spec_multidim,
                    bundle,
                    components,
                    dims=dims,
                    chunk_params=chunk_params,
                    purge_before=purge_before,
                )
                await _record(stats, bundle)
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(_worker(i)) for i in range(workers)]
    await asyncio.gather(*tasks)

    if finalize_bm25:
        try:
            components.bm25.finalize()
        except Exception:
            log.exception("bm25 finalize failed (continuing)")

    # 限速器快照（mimo 通过 async limiter.with_rate_limit 真实计数；
    # voyage sync embed 不走 async limiter，但 index_spec_multidim 完成时
    # 已通过 _record_voyage_usage 手动把 token / 请求数累加到 limiter.usage，
    # 这里 snapshot 拿到的就是累计真实速率，不再是 0）。
    _snapshot_limiters(pstats)

    pstats.elapsed_s = round(time.time() - t0, 2)
    return pstats


def _record_voyage_usage(stats: IndexStats) -> None:
    """把单 spec sync embed 的 token / request 数累加到 voyage limiter usage。

    sync `Embedder.embed_texts` 不走 async `limiter.with_rate_limit`，导致
    `PipelineStats.voyage_tokens_total / voyage_requests_total` 始终为 0
    （2026-05-16 M2 17 篇 POC handoff §3.4 P2 bug）。
    这里在每 spec 完成时手动 += 到 limiter.usage，pipeline 末尾 snapshot 即可
    拿到真实累计速率。

    线程安全：CPython int += 受 GIL 保护，多 worker `asyncio.to_thread` 并发安全。
    """
    if stats.embedding_tokens <= 0 and stats.embedding_calls <= 0:
        return
    try:
        usage = get_voyage_limiter().usage
        usage.tokens_used += int(stats.embedding_tokens)
        usage.requests_made += int(stats.embedding_calls)
    except Exception:  # pragma: no cover - 单测 reset 路径下可能短暂无单例
        log.debug("voyage usage backfill skipped", exc_info=True)


def _snapshot_limiters(pstats: PipelineStats) -> None:
    """从全局 voyage / mimo limiter 读快照回填到 PipelineStats。

    sync `index_specs` / async `pipeline_concurrent` 公用，避免各处重复实现。
    """
    try:
        mimo_snap = get_mimo_limiter().snapshot_usage()
        pstats.mimo_requests_total = mimo_snap.requests_made
    except Exception:
        log.debug("mimo limiter snapshot failed", exc_info=True)
    try:
        voyage_snap = get_voyage_limiter().snapshot_usage()
        pstats.voyage_tokens_total = voyage_snap.tokens_used
        pstats.voyage_requests_total = voyage_snap.requests_made
    except Exception:
        log.debug("voyage limiter snapshot failed", exc_info=True)


def _resolve_dead_letter_dir(explicit: str | Path | None) -> Path | None:
    if explicit is not None:
        p = Path(explicit)
        p.mkdir(parents=True, exist_ok=True)
        return p
    base = os.environ.get("INGEST_DATA_DIR")
    if not base:
        return None
    p = Path(base) / "failed"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_dead_letter(dl_dir: Path | None, spec_id: str, stats: IndexStats) -> None:
    if dl_dir is None:
        return
    ts = int(time.time())
    safe_spec = spec_id.replace("/", "_")
    out = dl_dir / f"{ts}_{safe_spec}.json"
    try:
        out.write_text(json.dumps(asdict(stats), ensure_ascii=False, indent=2))
    except Exception:  # pragma: no cover
        log.exception("dead_letter write failed for %s", spec_id)


def _dedupe_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """按 chunk_id 去重，保留首次出现。

    `chunk_id = uuid5(spec_id|clause|sha256(content)[:16])` 内容稳定 → 同 ID
    意味着 content 完全相同。这里保留第一个出现的副本，丢弃后续重复。
    """
    seen: set[str] = set()
    out: list[Chunk] = []
    for c in chunks:
        if c.chunk_id in seen:
            continue
        seen.add(c.chunk_id)
        out.append(c)
    return out


def index_stats_to_json(stats: IndexStats) -> dict:
    """CLI 输出 / 落盘用。"""
    return asdict(stats)


def pipeline_stats_to_json(stats: PipelineStats) -> dict:
    return asdict(stats)


# -------------------- 工厂便捷 --------------------


def build_components_from_env(
    *,
    provider: Provider = "voyage",
    qdrant_collection: str | None = None,
    bm25_dir: str | None = None,
    pg_enabled: bool = True,
    vision_resolver: ChunkerVisionResolver | None = None,
) -> IndexerComponents:
    """读 .env 一次性构造全部 components（pipeline-hf CLI 用）。

    `pg_enabled=False` 跳过 PG，给 CI / 仅 Qdrant 测试用。
    """
    embedder = Embedder.from_env(provider=provider)
    qdrant = QdrantWriter(
        provider=provider,
        dim=None,  # 由 embedder.warmup 探测
        collection_name=qdrant_collection,
    )
    bm25 = BM25Writer(provider=provider, base_dir=bm25_dir)
    pg: PgChunkMetaWriter | None = None
    if pg_enabled:
        try:
            pg = PgChunkMetaWriter.from_env(provider=provider)
        except Exception as exc:
            log.warning("pg writer disabled: %s", exc)
            pg = None
    return IndexerComponents(
        embedder=embedder,
        qdrant=qdrant,
        bm25=bm25,
        pg=pg,
        vision_resolver=vision_resolver,
    )


__all__ = [
    "Chunk",  # 导出方便外部 import
    "IndexerComponents",
    "build_components_from_env",
    "index_spec",
    "index_spec_multidim",
    "index_specs",
    "index_stats_to_json",
    "pipeline_concurrent",
    "pipeline_stats_to_json",
]
