"""eval/retrieval/retriever.py 离线单测（fake embedder + fake qdrant）。

不依赖 live LiteLLM / Qdrant；验证：
- collection name 推导
- search 走 vector → fake qdrant → 展开 Hit
- search_multidim 一次 embed 多 dim 派生
- payload → Hit 字段映射
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from eval.retrieval.client import EmbedResult
from eval.retrieval.retriever import Hit, Retriever, collection_name


class _FakeEmbedClient:
    """构造可预测的 embedding：第一维 = hash(text) % 100 / 100，其余 0。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def embed_query(self, text: str, *, dims: Sequence[int] = (2048, 1024)) -> EmbedResult:
        self.calls.append((text, tuple(int(d) for d in dims)))
        unique = sorted({int(d) for d in dims}, reverse=True)
        dim_main = unique[0]
        seed = (hash(text) & 0xFFFF) / 65535
        # 单位向量：第一维 = 1，其余 0
        base = [0.0] * dim_main
        base[0] = 1.0
        out = {dim_main: base}
        for sub in unique[1:]:
            head = base[:sub]
            # L2 norm = 1（base[0]=1）
            out[sub] = head
        return EmbedResult(
            vectors_by_dim=out,
            dim_main=dim_main,
            model="fake-voyage",
            prompt_tokens=int(seed * 10),
        )

    def close(self) -> None:
        pass


class _FakeQdrantPoint:
    def __init__(self, *, id: str, score: float, payload: dict) -> None:
        self.id = id
        self.score = score
        self.payload = payload


class _FakeQdrantQueryResponse:
    def __init__(self, points: list[_FakeQdrantPoint]) -> None:
        self.points = points


class _FakeQdrantClient:
    def __init__(self, *, fixed_points: dict[str, list[_FakeQdrantPoint]]) -> None:
        self._points = fixed_points
        self.queries: list[tuple[str, int, Any]] = []

    def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        with_payload: bool = True,
        query_filter: Any = None,
    ) -> _FakeQdrantQueryResponse:
        self.queries.append((collection_name, limit, query_filter))
        pts = self._points.get(collection_name, [])[:limit]
        return _FakeQdrantQueryResponse(pts)

    def close(self) -> None:
        pass


def _make_payload(
    *,
    chunk_id: str,
    spec_id: str,
    section_path: list[str],
    chunk_type: str = "section",
    content: str = "lorem ipsum",
    clause: str = "",
    section_title: str = "",
) -> dict:
    return {
        "chunk_id": chunk_id,
        "spec_id": spec_id,
        "section_path": section_path,
        "chunk_type": chunk_type,
        "content": content,
        "clause": clause,
        "section_title": section_title,
        "parent_section_id": None,
    }


class TestCollectionName:
    def test_default_prefix(self) -> None:
        assert collection_name("voyage", 2048) == "tgpp_chunks_voyage_d2048"
        assert collection_name("voyage", 1024) == "tgpp_chunks_voyage_d1024"
        assert collection_name("glm", 2048) == "tgpp_chunks_glm_d2048"

    def test_custom_prefix(self) -> None:
        assert collection_name("voyage", 1024, prefix="my_chunks") == "my_chunks_voyage_d1024"


class TestRetrieverSearch:
    def test_search_with_vector_basic(self) -> None:
        coll = "tgpp_chunks_voyage_d2048"
        points = [
            _FakeQdrantPoint(
                id="cid-1",
                score=0.95,
                payload=_make_payload(
                    chunk_id="cid-1", spec_id="38.331", section_path=["5", "3", "5"]
                ),
            ),
            _FakeQdrantPoint(
                id="cid-2",
                score=0.85,
                payload=_make_payload(chunk_id="cid-2", spec_id="23.501", section_path=["5", "6"]),
            ),
        ]
        qd = _FakeQdrantClient(fixed_points={coll: points})
        emb = _FakeEmbedClient()
        with Retriever(embedder=emb, qdrant=qd) as r:  # type: ignore[arg-type]
            vec = [1.0] + [0.0] * 2047
            hits = r.search_with_vector(vec, dim=2048, top_k=5)

        assert len(hits) == 2
        assert hits[0].chunk_id == "cid-1"
        assert hits[0].score == pytest.approx(0.95)
        assert hits[0].spec_id == "38.331"
        assert hits[0].section_path == ["5", "3", "5"]
        assert hits[0].chunk_type == "section"
        assert qd.queries[0][0] == coll
        assert qd.queries[0][1] == 5

    def test_vector_dim_mismatch_raises(self) -> None:
        qd = _FakeQdrantClient(fixed_points={})
        emb = _FakeEmbedClient()
        with (
            Retriever(embedder=emb, qdrant=qd) as r,  # type: ignore[arg-type]
            pytest.raises(ValueError, match=r"vector len \d+ != dim"),
        ):
            r.search_with_vector([1.0, 0.0, 0.0], dim=2048, top_k=5)

    def test_search_invokes_embed_then_qdrant(self) -> None:
        coll = "tgpp_chunks_voyage_d2048"
        qd = _FakeQdrantClient(
            fixed_points={
                coll: [
                    _FakeQdrantPoint(
                        id="x",
                        score=0.5,
                        payload=_make_payload(chunk_id="x", spec_id="38.331", section_path=["1"]),
                    )
                ]
            }
        )
        emb = _FakeEmbedClient()
        with Retriever(embedder=emb, qdrant=qd) as r:  # type: ignore[arg-type]
            hits = r.search("hello", dim=2048, top_k=3)
        assert len(hits) == 1
        assert emb.calls == [("hello", (2048,))]
        assert qd.queries[0][0] == coll
        assert qd.queries[0][1] == 3

    def test_search_multidim_single_embed(self) -> None:
        emb = _FakeEmbedClient()
        qd = _FakeQdrantClient(
            fixed_points={
                "tgpp_chunks_voyage_d2048": [
                    _FakeQdrantPoint(
                        id="a",
                        score=0.9,
                        payload=_make_payload(chunk_id="a", spec_id="38.331", section_path=["5"]),
                    )
                ],
                "tgpp_chunks_voyage_d1024": [
                    _FakeQdrantPoint(
                        id="b",
                        score=0.8,
                        payload=_make_payload(chunk_id="b", spec_id="23.501", section_path=["6"]),
                    )
                ],
            }
        )
        with Retriever(embedder=emb, qdrant=qd) as r:  # type: ignore[arg-type]
            out = r.search_multidim("hello", dims=(2048, 1024), top_k=5)
        assert set(out.keys()) == {2048, 1024}
        assert out[2048][0].chunk_id == "a"
        assert out[1024][0].chunk_id == "b"
        # 一次 embed 调用
        assert len(emb.calls) == 1
        assert emb.calls[0][1] == (2048, 1024)
        # 两次 qdrant 查询
        assert len(qd.queries) == 2

    def test_payload_to_hit_handles_string_section_path(self) -> None:
        coll = "tgpp_chunks_voyage_d1024"
        # section_path 是字符串而非 list（极端情况）— 应被强制转 [字符串]
        bad_payload = _make_payload(
            chunk_id="z", spec_id="38.300", section_path="5.3.1"  # type: ignore[arg-type]
        )
        qd = _FakeQdrantClient(
            fixed_points={coll: [_FakeQdrantPoint(id="z", score=1.0, payload=bad_payload)]}
        )
        emb = _FakeEmbedClient()
        with Retriever(embedder=emb, qdrant=qd) as r:  # type: ignore[arg-type]
            hits = r.search_with_vector([1.0] + [0.0] * 1023, dim=1024, top_k=1)
        assert isinstance(hits[0].section_path, list)
        assert len(hits[0].section_path) == 1
        assert hits[0].section_path[0] == "5.3.1"


class TestHitDataclass:
    def test_minimal(self) -> None:
        h = Hit(
            chunk_id="x",
            score=0.5,
            spec_id="38.331",
            clause="5.3.5.1",
            section_path=["5", "3", "5", "1"],
            section_title="Section",
            chunk_type="section",
            content="...",
        )
        assert h.parent_section_id is None
        assert h.raw_payload is None
