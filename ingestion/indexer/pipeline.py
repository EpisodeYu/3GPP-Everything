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

import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

from ingestion.chunker import ChunkParams, build_chunks
from ingestion.chunker.figure import VisionResolver as ChunkerVisionResolver
from ingestion.chunker.models import Chunk
from ingestion.hf_loader.models import SpecBundle

from .bm25_writer import BM25Writer
from .embedder import Embedder, embed_chunks
from .models import IndexStats, PipelineStats, Provider
from .pg_writer import PgChunkMetaWriter
from .qdrant_writer import QdrantWriter

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

    pstats.elapsed_s = round(time.time() - t0, 2)
    return pstats


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
    "index_specs",
    "index_stats_to_json",
    "pipeline_stats_to_json",
]
