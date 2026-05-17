"""Reranker 调用 LiteLLM rerank 并组装结果。"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import RetrievalError
from app.retrieval.models import RetrievedChunk
from app.retrieval.rerank import Reranker


class _StubLiteLLM:
    def __init__(self, ranks: list[dict[str, Any]] | Exception) -> None:
        self._ranks = ranks
        self.calls: list[dict[str, Any]] = []

    async def rerank(self, *, query, documents, model, top_k):  # type: ignore[no-untyped-def]
        self.calls.append(
            {"query": query, "documents": list(documents), "model": model, "top_k": top_k}
        )
        if isinstance(self._ranks, Exception):
            raise self._ranks
        return self._ranks


def _chunks() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=f"c{i}",
            spec_id="38.331",
            section_path=("5",),
            section_title=f"t{i}",
            chunk_type="text",
            content=f"doc {i}",
            fused_score=1.0 / (60 + i),
        )
        for i in range(4)
    ]


async def test_rerank_reorders_by_relevance_score() -> None:
    cands = _chunks()
    cli = _StubLiteLLM(
        [
            {"index": 2, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.81},
            {"index": 1, "relevance_score": 0.44},
        ]
    )
    r = Reranker(litellm_client=cli, model="rerank-2.5")  # type: ignore[arg-type]
    out = await r.rerank("query", cands, top_k=3)

    assert [c.chunk_id for c in out] == ["c2", "c0", "c1"]
    assert out[0].score_rerank == 0.95
    # fused_score 保持
    assert out[0].fused_score == cands[2].fused_score
    # 调用参数
    assert cli.calls[0]["top_k"] == 3
    assert cli.calls[0]["documents"] == ["doc 0", "doc 1", "doc 2", "doc 3"]


async def test_empty_input_short_circuits() -> None:
    cli = _StubLiteLLM([])
    r = Reranker(litellm_client=cli, model="m")  # type: ignore[arg-type]
    out = await r.rerank("q", [], top_k=5)
    assert out == []
    assert cli.calls == []


async def test_failure_raises_retrieval_error() -> None:
    cli = _StubLiteLLM(RuntimeError("boom"))
    r = Reranker(litellm_client=cli, model="m")  # type: ignore[arg-type]
    with pytest.raises(RetrievalError):
        await r.rerank("q", _chunks(), top_k=2)


async def test_out_of_range_index_skipped() -> None:
    cli = _StubLiteLLM([{"index": 99, "relevance_score": 0.9}])
    r = Reranker(litellm_client=cli, model="m")  # type: ignore[arg-type]
    out = await r.rerank("q", _chunks(), top_k=5)
    assert out == []
