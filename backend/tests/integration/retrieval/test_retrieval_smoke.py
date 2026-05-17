"""M4.0 retrieval smoke：3 个真实 query 能从 `tgpp_chunks_voyage_d1024` + BM25 拿回 top-50。

依赖：
- Qdrant 实例（settings.QDRANT_URL）含 `tgpp_chunks_voyage_d1024` collection
- BM25 持久化目录（settings.bm25_dir / by_spec/*.jsonl）
- LiteLLM proxy 可达 + voyage embedding 配置可用

不在 CI 默认跑（标记 `integration` + 显式要 LITELLM_API_KEY 才执行）。
预估成本：3 query × ~10 token ≈ 30 voyage token < 1 cent。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.llm.litellm_client import LiteLLMClient
from app.retrieval.dense import DenseRetriever
from app.retrieval.hybrid import rrf_merge
from app.retrieval.sparse import SparseRetriever

pytestmark = pytest.mark.integration


QUERIES = [
    "What is a PDU Session in 5G core?",
    "RRC connection establishment procedure between UE and gNB",
    "AMF selection function in 5G System",
]


def _bm25_available() -> bool:
    s = get_settings()
    return (Path(s.bm25_dir) / "by_spec").is_dir()


def _litellm_available() -> bool:
    return bool(get_settings().LITELLM_API_KEY.get_secret_value())


@pytest.fixture(scope="module")
def sparse_retriever() -> SparseRetriever:
    if not _bm25_available():
        pytest.skip("BM25 by_spec directory not available")
    return SparseRetriever.from_env()


@pytest.mark.asyncio
async def test_smoke_three_real_queries(sparse_retriever: SparseRetriever) -> None:
    if not _litellm_available():
        pytest.skip("LITELLM_API_KEY missing — cannot run dense retrieval smoke")

    s = get_settings()
    async with LiteLLMClient() as litellm:
        dense = DenseRetriever.from_env(embedder=litellm)
        try:
            for query in QUERIES:
                dense_hits = await dense.retrieve(query, top_k=s.RETRIEVAL_DENSE_TOP_K)
                sparse_hits = sparse_retriever.retrieve(
                    query, top_k=s.RETRIEVAL_SPARSE_TOP_K
                )
                merged = rrf_merge(
                    dense_hits, sparse_hits, k=s.RETRIEVAL_RRF_K, top_n=s.RETRIEVAL_FINAL_TOP_K
                )

                assert dense_hits, f"dense returned 0 hits for {query!r}"
                assert sparse_hits, f"sparse returned 0 hits for {query!r}"
                assert merged, f"merged returned 0 hits for {query!r}"
                # M4.0 验收：top-50 至少能拿到 ≥ 30 条（dense 与 sparse 几乎不会全重复）
                assert len(merged) >= 30, (
                    f"merged unique chunks < 30 for {query!r}: got {len(merged)}"
                )
                # 抽 top-1 检查必要字段
                top = merged[0]
                assert top.chunk_id
                assert top.spec_id
                assert top.content
                # 至少有一种 score 非空
                assert (top.score_dense is not None) or (top.score_sparse is not None)
        finally:
            await dense.close()


def test_smoke_sparse_only(sparse_retriever: SparseRetriever) -> None:
    """BM25 独立验证（不需要 LiteLLM；CI 没有 API key 时也能跑）。"""
    for query in QUERIES:
        hits = sparse_retriever.retrieve(query, top_k=50)
        assert hits, f"sparse returned 0 hits for {query!r}"
        assert len(hits) >= 30
        assert all(c.score_sparse is not None for c in hits)
