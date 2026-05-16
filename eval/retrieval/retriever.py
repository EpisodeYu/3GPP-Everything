"""Retriever：query → embed → Qdrant search → list[Hit]。

约定：
- collection 名严格按 `{prefix}_{provider}_d{dim}` 推导（与
  ingestion/indexer/qdrant_writer.collection_name_for_provider 一致；故意复制实现避免反向依赖）
- 只支持 dense-only（M3 维度决胜不引入 BM25 hybrid，避免变量混淆）
- payload 字段与 ingestion 写入侧 `_chunk_to_payload` 对齐：
    spec_id / spec_number / release / clause / section_path / section_title /
    chunk_type / parent_section_id / content
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient

from eval.settings import EvalSettings, get_settings

from .client import LiteLLMEmbedClient, get_qdrant_client

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Hit:
    """Qdrant 单次命中（已展开 payload 关键字段）。"""

    chunk_id: str
    score: float
    spec_id: str
    clause: str
    section_path: list[str]
    section_title: str
    chunk_type: str
    content: str
    parent_section_id: str | None = None
    raw_payload: dict[str, Any] | None = None


def collection_name(provider: str, dim: int, *, prefix: str = "tgpp_chunks") -> str:
    """`{prefix}_{provider}_d{dim}`（与 ingestion qdrant_writer 同算法）。"""
    return f"{prefix}_{provider}_d{dim}"


def _payload_to_hit(*, chunk_id: str, score: float, payload: dict[str, Any]) -> Hit:
    section_path = payload.get("section_path") or []
    if isinstance(section_path, str):
        section_path = [section_path]
    return Hit(
        chunk_id=str(payload.get("chunk_id") or chunk_id),
        score=float(score),
        spec_id=str(payload.get("spec_id") or ""),
        clause=str(payload.get("clause") or ""),
        section_path=[str(x) for x in section_path],
        section_title=str(payload.get("section_title") or ""),
        chunk_type=str(payload.get("chunk_type") or ""),
        content=str(payload.get("content") or ""),
        parent_section_id=(
            str(payload.get("parent_section_id"))
            if payload.get("parent_section_id") is not None
            else None
        ),
        raw_payload=payload,
    )


class Retriever:
    """Dense-only retriever。

    用法：
        with Retriever() as r:
            hits = r.search("What is PDU Session?", dim=2048, top_k=10)

    构造可注入：
        - embedder / qdrant：测试时塞 fake；生产 None 时按 .env 自建
        - settings：覆盖默认 EvalSettings
    """

    def __init__(
        self,
        *,
        provider: str | None = None,
        embedder: LiteLLMEmbedClient | None = None,
        qdrant: QdrantClient | None = None,
        collection_prefix: str | None = None,
        settings: EvalSettings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.provider = provider or self._settings.embedding_provider
        self._embedder = embedder
        self._owns_embedder = embedder is None
        self._qdrant = qdrant or get_qdrant_client(self._settings)
        self._owns_qdrant = qdrant is None
        self._prefix = collection_prefix or self._settings.qdrant_collection_prefix

    def close(self) -> None:
        import contextlib

        if self._owns_embedder and self._embedder is not None:
            self._embedder.close()
        if self._owns_qdrant:
            with contextlib.suppress(Exception):
                # qdrant-client 旧版本无 close 方法；其他清理失败也吞掉
                self._qdrant.close()

    def __enter__(self) -> Retriever:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_embedder(self) -> LiteLLMEmbedClient:
        if self._embedder is None:
            self._embedder = LiteLLMEmbedClient(settings=self._settings)
        return self._embedder

    def collection_for(self, dim: int) -> str:
        return collection_name(self.provider, dim, prefix=self._prefix)

    def search(
        self,
        query: str,
        *,
        dim: int,
        top_k: int = 20,
        spec_filter: Sequence[str] | None = None,
    ) -> list[Hit]:
        """对 query embed → Qdrant 查 → 展开为 Hit。

        - dim 必须 ∈ 已索引的 collection（M3 期默认 2048 / 1024）
        - spec_filter：可选，仅在指定 spec 内召回（用于 sanity 测试）
        """
        emb = self._ensure_embedder()
        result = emb.embed_query(query, dims=[dim])
        vec = result.vectors_by_dim[dim]
        return self.search_with_vector(vec, dim=dim, top_k=top_k, spec_filter=spec_filter)

    def search_multidim(
        self,
        query: str,
        *,
        dims: Sequence[int] = (2048, 1024),
        top_k: int = 20,
        spec_filter: Sequence[str] | None = None,
    ) -> dict[int, list[Hit]]:
        """对 query 一次 embed，按 dims 各自跑 Qdrant search → dim → list[Hit]。

        M3 维度决胜的核心入口：单次 embed API call，N 次 Qdrant search。
        """
        emb = self._ensure_embedder()
        result = emb.embed_query(query, dims=dims)
        out: dict[int, list[Hit]] = {}
        for dim in result.vectors_by_dim:
            out[dim] = self.search_with_vector(
                result.vectors_by_dim[dim], dim=dim, top_k=top_k, spec_filter=spec_filter
            )
        return out

    def search_with_vector(
        self,
        vector: Sequence[float],
        *,
        dim: int,
        top_k: int = 20,
        spec_filter: Sequence[str] | None = None,
    ) -> list[Hit]:
        """跳过 embedding 直接拿 vector 查 Qdrant（测试 / 复用 vector 时用）。"""
        from qdrant_client.http import models as qmodels

        if len(vector) != dim:
            raise ValueError(f"vector len {len(vector)} != dim {dim}")
        coll = self.collection_for(dim)
        flt: qmodels.Filter | None = None
        if spec_filter:
            flt = qmodels.Filter(
                should=[
                    qmodels.FieldCondition(key="spec_id", match=qmodels.MatchValue(value=s))
                    for s in spec_filter
                ]
            )
        t0 = time.perf_counter()
        # qdrant_client 1.11+ 推荐 query_points；search 仍兼容
        try:
            qp = self._qdrant.query_points(
                collection_name=coll,
                query=list(vector),
                limit=int(top_k),
                with_payload=True,
                query_filter=flt,
            )
            scored = qp.points
        except AttributeError:
            scored = self._qdrant.search(
                collection_name=coll,
                query_vector=list(vector),
                limit=int(top_k),
                with_payload=True,
                query_filter=flt,
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.debug(
            "qdrant.search dim=%d top_k=%d → %d hits (%.1f ms, coll=%s)",
            dim,
            top_k,
            len(scored),
            elapsed_ms,
            coll,
        )

        hits: list[Hit] = []
        for sp in scored:
            payload = sp.payload or {}
            hits.append(
                _payload_to_hit(chunk_id=str(sp.id), score=float(sp.score), payload=payload)
            )
        return hits


__all__ = ["Hit", "Retriever", "collection_name"]
