"""Indexer 数据契约。

只放数据结构，不做任何 IO / 业务。被 embedder / qdrant_writer / bm25_writer /
pg_writer / pipeline 共享。

字段口径来自 docs/03-development/02-ingestion-and-indexing.md §4.4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Provider = Literal["voyage", "glm", "openai"]


@dataclass(slots=True)
class EmbeddingBatchResult:
    """一次 embedding batch 调用的结果。

    `vectors[i]` 与传入的 `texts[i]` 一一对应；`dim` = len(vectors[0])（保持长度一致校验）。
    `prompt_tokens` 在 LiteLLM 返回 usage 时填充，否则为 0。
    """

    vectors: list[list[float]]
    dim: int
    model: str
    prompt_tokens: int = 0


@dataclass(slots=True)
class MultiDimEmbeddingResult:
    """单次 multidim embed 的结果（M2 §4.7）。

    `vectors_by_dim[dim][i]` = 第 i 个 chunk 在 dim 维度下的向量。
    `dim_main` = embedding API 实际请求的维度（最大那一档；其他档由 truncate+L2 renorm 派生）。
    """

    vectors_by_dim: dict[int, list[list[float]]]
    dim_main: int
    model: str
    prompt_tokens: int = 0

    @property
    def n(self) -> int:
        if not self.vectors_by_dim:
            return 0
        return len(next(iter(self.vectors_by_dim.values())))


@dataclass(slots=True)
class IndexStats:
    """单 spec 端到端 indexer 的统计，供 CLI / runner / 监控输出。"""

    spec_id: str
    chunks_total: int = 0
    chunks_indexed: int = 0
    chunks_skipped: int = 0
    vectors_dim: int = 0
    embedding_calls: int = 0
    embedding_tokens: int = 0
    qdrant_upserted: int = 0
    qdrant_upserted_by_dim: dict[int, int] = field(default_factory=dict)
    bm25_persisted: int = 0
    pg_upserted: int = 0
    chunks_by_type: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class PipelineStats:
    """跨多个 spec 的 pipeline-hf 整体统计。"""

    provider: str
    specs_attempted: int = 0
    specs_succeeded: int = 0
    specs_failed: int = 0
    chunks_total: int = 0
    qdrant_upserted: int = 0
    qdrant_upserted_by_dim: dict[int, int] = field(default_factory=dict)
    embedding_tokens: int = 0
    elapsed_s: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)
    # M2 §4.8 limiter 快照（pipeline_concurrent 跑完后回填；sequential 路径保持 0）
    voyage_tokens_total: int = 0
    voyage_requests_total: int = 0
    mimo_requests_total: int = 0
