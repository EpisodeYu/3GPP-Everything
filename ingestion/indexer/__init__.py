"""Indexer 模块（GSMA chunks → Voyage/GLM embedding → Qdrant + BM25 + PG）。

公共入口：

- `Embedder`            LiteLLM `/embeddings` 客户端（voyage / glm）
- `QdrantWriter`        collection 创建 + payload 索引 + 幂等 upsert
- `BM25Writer`          chunks.jsonl + meta.json 持久化（backend 加载用）
- `PgChunkMetaWriter`   PG `chunks_meta` CREATE TABLE IF NOT EXISTS + DELETE-then-INSERT
- `IndexerComponents`   单 spec / 多 spec pipeline 用的"接线包"
- `index_spec`          单 spec 端到端编排
- `index_specs`         跨 spec 批量编排（pipeline-hf 内核）
- `build_components_from_env`  按 .env 构造一组 components

详见 docs/03-development/02-ingestion-and-indexing.md §4.4 / §4.7。
"""

from .bm25_writer import BM25Writer, default_bm25_dir
from .embedder import (
    DEFAULT_BATCH_SIZE,
    Embedder,
    EmbeddingError,
    embed_chunks,
)
from .models import (
    EmbeddingBatchResult,
    IndexStats,
    PipelineStats,
    Provider,
)
from .pg_writer import (
    PgChunkMetaWriter,
    build_engine,
    chunks_meta_table,
    default_database_url,
)
from .pipeline import (
    IndexerComponents,
    build_components_from_env,
    index_spec,
    index_specs,
    index_stats_to_json,
    pipeline_stats_to_json,
)
from .qdrant_writer import (
    DEFAULT_PAYLOAD_INDEXED_FIELDS,
    QdrantWriter,
    collection_name_for_provider,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_PAYLOAD_INDEXED_FIELDS",
    "BM25Writer",
    "Embedder",
    "EmbeddingBatchResult",
    "EmbeddingError",
    "IndexStats",
    "IndexerComponents",
    "PgChunkMetaWriter",
    "PipelineStats",
    "Provider",
    "QdrantWriter",
    "build_components_from_env",
    "build_engine",
    "chunks_meta_table",
    "collection_name_for_provider",
    "default_bm25_dir",
    "default_database_url",
    "embed_chunks",
    "index_spec",
    "index_specs",
    "index_stats_to_json",
    "pipeline_stats_to_json",
]
