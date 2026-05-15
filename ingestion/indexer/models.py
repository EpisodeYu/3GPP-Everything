"""Indexer 数据契约。

只放数据结构，不做任何 IO / 业务。被 embedder / qdrant_writer / bm25_writer /
pg_writer / pipeline 共享。

字段口径来自 docs/03-development/02-ingestion-and-indexing.md §4.4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Provider = Literal["voyage", "glm"]


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
    embedding_tokens: int = 0
    elapsed_s: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)
