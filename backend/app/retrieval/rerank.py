"""Voyage rerank-2.5（透过 LiteLLM proxy）。

输入：query + RetrievedChunk list（任意来源，通常是 hybrid 融合后的 top-50）；
输出：按 `score_rerank` 降序的前 `top_k` 个；`fused_score` 保留不变（caller 决定
后续展示按哪个分数）。

错误处理：rerank 失败时不阻塞主路径 —— caller 可选择捕获 RetrievalError 后退回到
fused_score 排序。这里直接抛，由 caller 决定。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.core.config import Settings, get_settings
from app.core.errors import RetrievalError
from app.llm.litellm_client import LiteLLMClient

from .models import RetrievedChunk

log = logging.getLogger(__name__)


class Reranker:
    def __init__(
        self,
        *,
        litellm_client: LiteLLMClient,
        model: str | None = None,
    ) -> None:
        self._client = litellm_client
        self._model = model or get_settings().VOYAGE_RERANK_MODEL

    @classmethod
    def from_env(
        cls, *, litellm_client: LiteLLMClient, settings: Settings | None = None
    ) -> Reranker:
        s = settings or get_settings()
        return cls(litellm_client=litellm_client, model=s.VOYAGE_RERANK_MODEL)

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedChunk],
        *,
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        if not candidates:
            return []
        documents = [c.content for c in candidates]
        try:
            ranks = await self._client.rerank(
                query=query,
                documents=documents,
                model=self._model,
                top_k=top_k,
            )
        except Exception as exc:
            raise RetrievalError(f"rerank failed: {exc}") from exc

        out: list[RetrievedChunk] = []
        for r in ranks[:top_k]:
            idx = int(r["index"])
            if idx < 0 or idx >= len(candidates):
                continue
            src = candidates[idx]
            out.append(
                RetrievedChunk(
                    chunk_id=src.chunk_id,
                    spec_id=src.spec_id,
                    section_path=src.section_path,
                    section_title=src.section_title,
                    chunk_type=src.chunk_type,
                    content=src.content,
                    score_dense=src.score_dense,
                    score_sparse=src.score_sparse,
                    score_rerank=float(r["relevance_score"]),
                    fused_score=src.fused_score,
                    extra=dict(src.extra),
                )
            )
        return out
