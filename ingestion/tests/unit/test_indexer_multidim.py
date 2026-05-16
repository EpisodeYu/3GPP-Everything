"""Embedder.embed_texts_multidim + QdrantWriter multidim 单测（M2 §4.7 B3）。

覆盖：
- truncate + L2 renorm 数学正确（norm == 1.0）
- multidim 一次 API 调用 + 派生其他维度
- multidim API 调用透传 dimensions=max(dims)
- ensure_collections 创建 `_d{dim}` 命名 + idempotent
- upsert_multidim 跨 collection 写入正确条数
- collection_name_for_provider 带 dim 后缀
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from qdrant_client import QdrantClient

from ingestion.indexer.embedder import (
    DEFAULT_MULTIDIM_DIMS,
    Embedder,
    EmbeddingError,
    _LiteLLMEmbeddingClient,
    _truncate_and_renorm,
)
from ingestion.indexer.models import MultiDimEmbeddingResult
from ingestion.indexer.qdrant_writer import (
    QdrantWriter,
    collection_name_for_provider,
)

# -------------------- helpers --------------------


class _StubMDHttp(_LiteLLMEmbeddingClient):
    """记录每次 embed 的 dimensions 参数，按队列吐 payload。"""

    def __init__(self, *, responses: list[object]) -> None:
        self.base_url = "http://stub"
        self.api_key = "stub"
        self._owns_client = False
        self._client = None  # type: ignore[assignment]
        self._responses = list(responses)
        self.calls: list[tuple[str, list[str], int | None]] = []

    def embed(self, *, model: str, inputs, dimensions: int | None = None):  # type: ignore[override]
        self.calls.append((model, list(inputs), dimensions))
        if not self._responses:
            raise AssertionError("StubMDHttp out of responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _payload(vectors, *, prompt_tokens: int = 0, model: str = "voyage-4-large"):
    return {
        "model": model,
        "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
        "usage": {"prompt_tokens": prompt_tokens},
    }


def _l2_norm(v):
    return math.sqrt(sum(x * x for x in v))


# -------------------- _truncate_and_renorm --------------------


def test_truncate_and_renorm_unit_norm() -> None:
    v = [3.0, 4.0, 0.0, 0.0, 0.0]  # head [3,4] norm 5 → [.6,.8]
    out = _truncate_and_renorm(v, 2)
    assert out == pytest.approx([0.6, 0.8])
    assert _l2_norm(out) == pytest.approx(1.0)


def test_truncate_and_renorm_full_dim_normalizes() -> None:
    # 2048-dim 模拟：取前 1024 维 norm 必为 1
    n = 2048
    v = [0.1] * n
    out = _truncate_and_renorm(v, 1024)
    assert len(out) == 1024
    assert _l2_norm(out) == pytest.approx(1.0, rel=1e-9)


def test_truncate_and_renorm_zero_vec_returns_zero_head() -> None:
    out = _truncate_and_renorm([0.0, 0.0, 0.0], 2)
    assert out == [0.0, 0.0]


def test_truncate_and_renorm_target_too_large_raises() -> None:
    with pytest.raises(EmbeddingError):
        _truncate_and_renorm([0.1, 0.2], 5)


# -------------------- Embedder.embed_texts_multidim --------------------


def test_multidim_makes_single_api_call_with_max_dim() -> None:
    # 模型实际返回 4 维（mock）；dims=[4,2] → 一次 API 4 维 + 客户端 truncate 2 维
    vec_main = [[0.6, 0.8, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]
    http = _StubMDHttp(responses=[_payload(vec_main, prompt_tokens=10)])
    with Embedder(http_client=http, model="voyage-test") as emb:
        res = emb.embed_texts_multidim(["a", "b"], dims=[4, 2])
    # 一次调用
    assert len(http.calls) == 1
    # dimensions 必须 = max(dims) = 4
    assert http.calls[0][2] == 4
    # 双维度向量
    assert isinstance(res, MultiDimEmbeddingResult)
    assert res.dim_main == 4
    assert sorted(res.vectors_by_dim) == [2, 4]
    assert res.vectors_by_dim[4] == vec_main
    # 派生 2 维：[0.6, 0.8] norm 1；[1, 0] norm 1
    assert res.vectors_by_dim[2][0] == pytest.approx([0.6, 0.8])
    assert res.vectors_by_dim[2][1] == pytest.approx([1.0, 0.0])
    assert res.prompt_tokens == 10
    assert res.n == 2


def test_multidim_default_dims_2048_1024() -> None:
    # 默认 (2048, 1024) — 模拟用 8/4 简化
    assert DEFAULT_MULTIDIM_DIMS == (2048, 1024)


def test_multidim_dim_already_set_restored_after_call() -> None:
    """调用 multidim 不应永久改变 self.dimensions。"""
    vec = [[1.0, 0.0, 0.0, 0.0]]
    http = _StubMDHttp(responses=[_payload(vec)])
    emb = Embedder(http_client=http, model="m", dimensions=999)
    emb.embed_texts_multidim(["x"], dims=[4, 2])
    assert emb.dimensions == 999  # 恢复


def test_multidim_empty_input_returns_empty_per_dim() -> None:
    http = _StubMDHttp(responses=[])
    with Embedder(http_client=http) as emb:
        res = emb.embed_texts_multidim([], dims=[4, 2])
    assert res.vectors_by_dim == {4: [], 2: []}
    assert res.dim_main == 4
    assert http.calls == []


def test_multidim_sub_dim_too_big_raises() -> None:
    # dims contains dim > main; impossible
    vec = [[0.1, 0.2]]
    http = _StubMDHttp(responses=[_payload(vec)])
    with Embedder(http_client=http) as emb, pytest.raises(EmbeddingError):
        # 主调返回 dim=2，但 caller 要求 4 维 sub —— 实际逻辑：max(dims)=4 是主，2 是 sub
        # 这里测的是：API 实际返回 < max(dims) 时抛
        emb.embed_texts_multidim(["x"], dims=[4, 2])


def test_multidim_dims_dedup_and_sort() -> None:
    vec = [[0.6, 0.8, 0.0, 0.0]]
    http = _StubMDHttp(responses=[_payload(vec)])
    with Embedder(http_client=http) as emb:
        res = emb.embed_texts_multidim(["x"], dims=[2, 4, 2, 4])
    # 去重后只剩 [4,2]
    assert sorted(res.vectors_by_dim) == [2, 4]


def test_multidim_invalid_dims_raise() -> None:
    http = _StubMDHttp(responses=[])
    with Embedder(http_client=http) as emb:
        with pytest.raises(EmbeddingError):
            emb.embed_texts_multidim(["x"], dims=[])
        with pytest.raises(EmbeddingError):
            emb.embed_texts_multidim(["x"], dims=[0, 1])


# -------------------- collection_name_for_provider with dim --------------------


def test_collection_name_with_dim_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QDRANT_COLLECTION_PREFIX", raising=False)
    assert collection_name_for_provider("voyage", dim=2048) == "tgpp_chunks_voyage_d2048"
    assert collection_name_for_provider("voyage", dim=1024) == "tgpp_chunks_voyage_d1024"
    # 自定义 prefix
    assert collection_name_for_provider("voyage", prefix="x", dim=512) == "x_voyage_d512"
    # dim=None 退回旧名（向后兼容）
    assert collection_name_for_provider("voyage") == "tgpp_chunks_voyage"


# -------------------- QdrantWriter ensure_collections + upsert_multidim --------------------


@dataclass(slots=True)
class _Chunk:
    chunk_id: str
    spec_id: str = "38.331"
    spec_uid: str | None = "38331"
    spec_number: str = "38.331"
    spec_type: str = "TS"
    release: str = "Rel-19"
    series: str = "38"
    title: str = "RRC"
    chunk_type: str = "text"
    clause: str = "5.2.1"
    section_path: tuple[str, ...] = ("5", "2", "1")
    section_title: str = "x"
    parent_section_id: str = "p1"
    parent_section_chars: int = 100
    document_order: int = 0
    content: str = "c"
    raw_extra: dict = field(default_factory=dict)
    cross_refs: list[str] = field(default_factory=list)
    source: str = "test"
    source_version: str = "v1"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _md_writer(prefix: str = "tmd") -> QdrantWriter:
    return QdrantWriter(
        client=QdrantClient(":memory:"),
        provider="voyage",
        collection_prefix=prefix,
    )


def _fake_chunks(n: int, *, spec_id: str = "38.331") -> list[_Chunk]:
    import uuid as _uuid

    ns = _uuid.uuid5(_uuid.NAMESPACE_URL, f"md|{spec_id}")
    return [
        _Chunk(chunk_id=str(_uuid.uuid5(ns, f"{i}")), spec_id=spec_id, document_order=i)
        for i in range(n)
    ]


def test_ensure_collections_creates_per_dim() -> None:
    w = _md_writer()
    out = w.ensure_collections([4, 2])
    assert out == {4: "tmd_voyage_d4", 2: "tmd_voyage_d2"}
    # idempotent
    out2 = w.ensure_collections([4, 2])
    assert out2 == out
    # 真存在
    assert w._client.collection_exists("tmd_voyage_d4")
    assert w._client.collection_exists("tmd_voyage_d2")


def test_ensure_collections_empty_dims_raises() -> None:
    w = _md_writer()
    with pytest.raises(RuntimeError):
        w.ensure_collections([])


def test_upsert_multidim_writes_to_per_dim_collection() -> None:
    w = _md_writer("tup")
    w.ensure_collections([4, 2])
    chunks = _fake_chunks(3)
    vectors_by_dim = {
        4: [[0.1, 0.2, 0.3, 0.4]] * 3,
        2: [[0.6, 0.8]] * 3,
    }
    counts = w.upsert_multidim(chunks, vectors_by_dim)
    assert counts == {4: 3, 2: 3}
    # 每个 collection 各 3
    assert w.count_multidim() == {4: 3, 2: 3}
    # spec 过滤
    assert w.count_multidim(spec_id="38.331") == {4: 3, 2: 3}
    assert w.count_multidim(spec_id="other") == {4: 0, 2: 0}


def test_upsert_multidim_requires_ensure_first() -> None:
    w = _md_writer("tneed")
    chunks = _fake_chunks(1)
    with pytest.raises(RuntimeError):
        w.upsert_multidim(chunks, {4: [[0.1, 0.2, 0.3, 0.4]]})


def test_upsert_multidim_dim_not_in_ensured_raises() -> None:
    w = _md_writer("tmissdim")
    w.ensure_collections([4])
    chunks = _fake_chunks(1)
    with pytest.raises(RuntimeError, match="not in ensure_collections"):
        w.upsert_multidim(chunks, {2: [[0.6, 0.8]]})


def test_upsert_multidim_length_mismatch_raises() -> None:
    w = _md_writer("tlenmis")
    w.ensure_collections([4])
    chunks = _fake_chunks(2)
    with pytest.raises(ValueError):
        w.upsert_multidim(chunks, {4: [[0.1, 0.2, 0.3, 0.4]]})  # vec 1, chunks 2


def test_upsert_multidim_chunk_id_consistent_across_collections() -> None:
    """同一 chunk 在两个 collection 应拿到同 chunk_id（uuid5），便于 small2big 配对。"""
    w = _md_writer("tids")
    w.ensure_collections([4, 2])
    chunks = _fake_chunks(2)
    w.upsert_multidim(
        chunks,
        {4: [[1.0, 0, 0, 0], [0, 1, 0, 0]], 2: [[1.0, 0], [0, 1]]},
    )
    pts4, _ = w._client.scroll(collection_name="tids_voyage_d4", with_payload=True, limit=10)
    pts2, _ = w._client.scroll(collection_name="tids_voyage_d2", with_payload=True, limit=10)
    ids4 = sorted(p.payload["chunk_id"] for p in pts4)
    ids2 = sorted(p.payload["chunk_id"] for p in pts2)
    assert ids4 == ids2 == sorted(c.chunk_id for c in chunks)


def test_purge_spec_multidim_removes_from_both() -> None:
    w = _md_writer("tpurge")
    w.ensure_collections([4, 2])
    chunks_a = _fake_chunks(3, spec_id="A.A")
    chunks_b = _fake_chunks(2, spec_id="B.B")
    w.upsert_multidim(
        chunks_a + chunks_b,
        {
            4: [[1.0, 0, 0, 0]] * 5,
            2: [[1.0, 0]] * 5,
        },
    )
    assert w.count_multidim() == {4: 5, 2: 5}
    removed = w.purge_spec_multidim("A.A")
    assert removed == {4: 3, 2: 3}
    assert w.count_multidim() == {4: 2, 2: 2}


def test_upsert_multidim_empty_chunks_returns_zero_per_dim() -> None:
    w = _md_writer("tempty")
    w.ensure_collections([4, 2])
    out = w.upsert_multidim([], {4: [], 2: []})
    assert out == {4: 0, 2: 0}


def test_upsert_multidim_partial_dim_subset() -> None:
    """caller 只 upsert 部分 dim 时，未传的 dim 保持空。"""
    w = _md_writer("tpartial")
    w.ensure_collections([4, 2])
    chunks = _fake_chunks(2)
    out = w.upsert_multidim(chunks, {4: [[0.1, 0.2, 0.3, 0.4]] * 2})
    assert out == {4: 2}
    assert w.count_multidim() == {4: 2, 2: 0}
