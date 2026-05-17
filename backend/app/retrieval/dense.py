"""Dense 向量检索（Qdrant async）。

直接用 qdrant-client.AsyncQdrantClient + LiteLLMClient.embed()，**不**经 LlamaIndex
VectorStoreIndex。原因：

- LlamaIndex VectorStoreIndex 主要价值在 ingest 路径（构建索引）；ingestion 端已经
  直接用 qdrant-client 写入，不需要再过 LlamaIndex
- 检索路径需要的只是 `embed(query) → qdrant.query_points`，自己 30 行写完
- 减少 langchain/llama-index 在 hot path 上的反射开销

收益：
- `score_dense` 直接从 qdrant 拿，不再过任何抽象层
- 测试可注入 stub client / stub embedder

返回 `RetrievedChunk.score_dense` = qdrant 返回的 cosine score（越大越相关）；其它
分数字段未赋值。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.core.config import Settings, get_settings
from app.core.errors import RetrievalError
from app.llm.litellm_client import LiteLLMClient

from .models import RetrievedChunk

log = logging.getLogger(__name__)


class DenseRetriever:
    def __init__(
        self,
        *,
        qdrant_client: AsyncQdrantClient,
        embedder: LiteLLMClient,
        collection: str,
        dimensions: int,
    ) -> None:
        self._qdrant = qdrant_client
        self._embedder = embedder
        self._collection = collection
        self._dimensions = dimensions

    @classmethod
    def from_env(
        cls,
        *,
        embedder: LiteLLMClient,
        settings: Settings | None = None,
    ) -> DenseRetriever:
        s = settings or get_settings()
        api_key = s.QDRANT_API_KEY.get_secret_value() or None
        qclient = AsyncQdrantClient(url=s.QDRANT_URL, api_key=api_key)
        return cls(
            qdrant_client=qclient,
            embedder=embedder,
            collection=s.qdrant_collection,
            dimensions=s.EMBEDDING_DIMENSIONS,
        )

    async def close(self) -> None:
        await self._qdrant.close()

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 30,
        filter_spec_ids: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]:
        """对单 query 拿 top_k 向量近邻。"""
        try:
            emb_resp = await self._embedder.embed([query], dimensions=self._dimensions)
        except Exception as exc:
            raise RetrievalError(f"dense embed failed: {exc}") from exc
        data = emb_resp.get("data") or []
        if not data:
            raise RetrievalError("dense embed returned empty data")
        vector = list(data[0].get("embedding") or [])
        if not vector:
            raise RetrievalError("dense embed returned empty vector")
        return await self.retrieve_by_vector(vector, top_k=top_k, filter_spec_ids=filter_spec_ids)

    async def retrieve_by_vector(
        self,
        vector: Sequence[float],
        *,
        top_k: int = 30,
        filter_spec_ids: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]:
        query_filter = _build_filter(filter_spec_ids)
        try:
            resp = await self._qdrant.query_points(
                collection_name=self._collection,
                query=list(vector),
                limit=top_k,
                query_filter=query_filter,
                with_payload=True,
            )
        except Exception as exc:
            raise RetrievalError(f"qdrant query_points failed: {exc}") from exc

        return [_point_to_chunk(p) for p in resp.points]


def _build_filter(spec_ids: Sequence[str] | None) -> qmodels.Filter | None:
    if not spec_ids:
        return None
    return qmodels.Filter(
        must=[qmodels.FieldCondition(key="spec_id", match=qmodels.MatchAny(any=list(spec_ids)))]
    )


def _point_to_chunk(point: Any) -> RetrievedChunk:
    payload: dict[str, Any] = dict(point.payload or {})
    section_path = payload.get("section_path") or []
    return RetrievedChunk(
        chunk_id=str(payload.get("chunk_id") or point.id),
        spec_id=str(payload.get("spec_id") or ""),
        section_path=tuple(str(x) for x in section_path),
        section_title=str(payload.get("section_title") or ""),
        chunk_type=str(payload.get("chunk_type") or "text"),
        content=str(payload.get("content") or ""),
        score_dense=float(point.score) if point.score is not None else None,
        extra={
            k: v
            for k, v in payload.items()
            if k
            not in {
                "chunk_id",
                "spec_id",
                "section_path",
                "section_title",
                "chunk_type",
                "content",
            }
        },
    )
