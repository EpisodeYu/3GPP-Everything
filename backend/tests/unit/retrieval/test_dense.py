"""DenseRetriever 行为（mock qdrant + mock LiteLLM）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.core.errors import RetrievalError
from app.retrieval.dense import DenseRetriever


@dataclass
class _StubPoint:
    id: str
    score: float
    payload: dict[str, Any]


class _StubQueryResponse:
    def __init__(self, points: list[_StubPoint]) -> None:
        self.points = points


class _StubQdrant:
    def __init__(self, points: list[_StubPoint]) -> None:
        self._points = points
        self.calls: list[dict[str, Any]] = []

    async def query_points(self, **kwargs: Any) -> _StubQueryResponse:
        self.calls.append(kwargs)
        return _StubQueryResponse(self._points[: kwargs.get("limit", len(self._points))])

    async def close(self) -> None:
        pass


class _StubEmbedder:
    def __init__(self, vector: list[float]) -> None:
        self._vec = vector
        self.calls: list[dict[str, Any]] = []

    async def embed(self, inputs, *, dimensions=None):  # type: ignore[no-untyped-def]
        self.calls.append({"inputs": list(inputs), "dimensions": dimensions})
        return {"data": [{"index": 0, "embedding": self._vec}]}


async def test_retrieve_embeds_and_calls_qdrant() -> None:
    points = [
        _StubPoint(
            id="p1",
            score=0.91,
            payload={
                "chunk_id": "c1",
                "spec_id": "38.331",
                "section_path": ["5", "3"],
                "section_title": "RRC",
                "chunk_type": "text",
                "content": "rrc connection",
                "extra_field": "ok",
            },
        )
    ]
    qdrant = _StubQdrant(points)
    embedder = _StubEmbedder(vector=[0.1] * 1024)
    r = DenseRetriever(
        qdrant_client=qdrant,  # type: ignore[arg-type]
        embedder=embedder,  # type: ignore[arg-type]
        collection="tgpp_chunks_voyage_d1024",
        dimensions=1024,
    )

    out = await r.retrieve("RRC connection", top_k=5)
    assert embedder.calls[0]["dimensions"] == 1024
    assert qdrant.calls[0]["collection_name"] == "tgpp_chunks_voyage_d1024"
    assert qdrant.calls[0]["limit"] == 5
    assert qdrant.calls[0]["query_filter"] is None
    assert out[0].chunk_id == "c1"
    assert out[0].section_path == ("5", "3")
    assert out[0].score_dense == 0.91
    assert out[0].extra == {"extra_field": "ok"}


async def test_filter_spec_ids_passed_to_qdrant() -> None:
    qdrant = _StubQdrant([])
    embedder = _StubEmbedder(vector=[0.0] * 4)
    r = DenseRetriever(
        qdrant_client=qdrant,  # type: ignore[arg-type]
        embedder=embedder,  # type: ignore[arg-type]
        collection="c",
        dimensions=4,
    )
    await r.retrieve("q", top_k=3, filter_spec_ids=["38.331", "23.501"])
    qf = qdrant.calls[0]["query_filter"]
    assert qf is not None
    # serialize to dict to avoid pydantic / dataclass coupling
    j = qf.model_dump()
    assert j["must"][0]["key"] == "spec_id"
    assert set(j["must"][0]["match"]["any"]) == {"38.331", "23.501"}


async def test_embed_failure_raises_retrieval_error() -> None:
    class _BadEmbedder:
        async def embed(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("upstream 503")

    r = DenseRetriever(
        qdrant_client=_StubQdrant([]),  # type: ignore[arg-type]
        embedder=_BadEmbedder(),  # type: ignore[arg-type]
        collection="c",
        dimensions=4,
    )
    with pytest.raises(RetrievalError):
        await r.retrieve("q")
