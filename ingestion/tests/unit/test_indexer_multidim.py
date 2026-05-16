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


def test_embed_texts_dimensions_arg_overrides_self() -> None:
    """method kwarg `dimensions=` 优先于 self.dimensions，且不污染 self（race fix）。"""
    vec = [[0.1, 0.2, 0.3, 0.4]]
    http = _StubMDHttp(responses=[_payload(vec), _payload(vec)])
    with Embedder(http_client=http, dimensions=2048) as emb:
        emb.embed_texts(["a"], dimensions=1024)
        emb.embed_texts(["b"])  # 走 self.dimensions
    assert http.calls[0][2] == 1024  # 显式 kwarg 生效
    assert http.calls[1][2] == 2048  # fallback 到 self.dimensions
    assert emb.dimensions == 2048  # self 未被 mutate


def test_multidim_concurrent_threads_no_race() -> None:
    """16 篇 POC 暴露的 race condition：3 worker 共享 embedder 调 multidim 时
    每次 API 调用的 dimensions 必须是该 worker 自己请求的 max(dims)，
    不能被另一个 worker 的 `self.dimensions = N` 串掉。

    旧实现 mutate `self.dimensions`，本测试在 race window 强制触发；新实现
    用 method param 传 dimensions，应稳定通过。
    """
    import threading
    import time

    class _RaceHttp(_LiteLLMEmbeddingClient):
        """每次 embed 按 caller 请求的 dimensions 返回对应长度向量；sleep 制造 race window。"""

        def __init__(self) -> None:
            self.base_url = "http://stub"
            self.api_key = "stub"
            self._owns_client = False
            self._client = None  # type: ignore[assignment]
            self.calls: list[int | None] = []
            self._lock = threading.Lock()

        def embed(self, *, model: str, inputs, dimensions: int | None = None):  # type: ignore[override]
            with self._lock:
                self.calls.append(dimensions)
            time.sleep(0.01)  # 让多线程跨越 race 窗口
            # 按 caller 请求的 dim 返回对应长度的简单向量（norm 必然能 renorm）
            n = dimensions or 4
            vec = [1.0] + [0.0] * (n - 1)
            return _payload([vec for _ in inputs])

    http = _RaceHttp()
    # 注意：实际 voyage-4-large 上限 2048；测试用更小维度模拟，只关心 dimensions 透传
    with Embedder(http_client=http) as emb:
        dims_options = [[8, 4], [4, 2], [8, 2]]
        results: list[Exception | None] = [None] * 24

        def _worker(idx: int) -> None:
            try:
                emb.embed_texts_multidim(
                    [f"text-{idx}"], dims=dims_options[idx % len(dims_options)]
                )
            except Exception as exc:
                results[idx] = exc

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # 所有 worker 都成功（无 race 触发的 EmbeddingError）
    errs = [r for r in results if r is not None]
    assert errs == [], f"race exposed: {len(errs)}/24 failed: {errs[:3]}"
    # 每次 API call 的 dimensions 必须是该 worker dims 选项中的 max
    valid_max_dims = {max(d) for d in dims_options}
    assert all(d in valid_max_dims for d in http.calls), http.calls
    assert len(http.calls) == 24  # 每 worker 一次 API 调用
    # self 未被任何 worker 污染
    assert emb.dimensions is None


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


def test_count_uses_main_dim_collection_in_multidim_mode() -> None:
    """handoff §3.2 P1 回归：multidim 模式下 count() 必须自动用主 dim collection。

    旧实现永远查 `self.collection_name`（无 _d 后缀），multidim 路径下
    它从未被创建 → count 永远 0 → `--skip-indexed` 失效（POC 主跑期未触发只是因为
    spec_ids 已显式排除）。
    """
    w = _md_writer("tcount_main")
    # 默认 collection_name = "tcount_main_voyage"（无 dim 后缀，multidim 模式下不建）
    base_name = w.collection_name
    assert not base_name.endswith("_d4")
    w.ensure_collections([4, 2])
    chunks = _fake_chunks(3)
    w.upsert_multidim(chunks, {4: [[0.1, 0.2, 0.3, 0.4]] * 3, 2: [[0.6, 0.8]] * 3})
    # base 名（无 dim）从未创建
    assert not w._client.collection_exists(base_name)
    # count 应自动用主 dim（max=4）collection
    assert w.count() == 3
    assert w.count(spec_id="38.331") == 3
    assert w.count(spec_id="other") == 0


def test_count_explicit_collection_name_overrides() -> None:
    """count(collection_name=...) 显式覆盖：可指定查任意 dim collection。"""
    w = _md_writer("tcount_explicit")
    w.ensure_collections([4, 2])
    chunks = _fake_chunks(2)
    w.upsert_multidim(chunks, {4: [[1.0, 0, 0, 0]] * 2, 2: [[1.0, 0]] * 2})
    # 显式查 d2 collection
    assert w.count(collection_name="tcount_explicit_voyage_d2") == 2
    # 不存在的 collection_name 返回 0（不抛）
    assert w.count(collection_name="never_created") == 0


def test_count_falls_back_to_self_collection_name_in_single_dim_mode() -> None:
    """单 dim 路径（ensure_collection，非 ensure_collections）保持原行为。"""
    w = QdrantWriter(
        client=QdrantClient(":memory:"),
        provider="voyage",
        dim=4,
        collection_name="single_dim_test",
    )
    w.ensure_collection()
    chunks = _fake_chunks(2)
    w.upsert_chunks(chunks, [[0.1, 0.2, 0.3, 0.4]] * 2)
    # _collections_by_dim 未被填充，count 仍走 self.collection_name 旧逻辑
    assert w._collections_by_dim == {}
    assert w.count() == 2
    assert w.count(spec_id="38.331") == 2


# -------------------- purge_spec multidim fix（与 count fix 同源 §3.2） --------------------


def test_purge_spec_uses_main_dim_collection_in_multidim_mode() -> None:
    """multidim 模式下 purge_spec() 默认应走主 dim collection。

    旧实现永远 short-circuit `self.collection_name`（无 _d 后缀）→ 不存在 → return 0
    → 静默 no-op。回归保证 M3→M6 之间 `ingestion purge-spec` CLI 能清干净。
    """
    w = _md_writer("tpurge_main")
    base_name = w.collection_name
    w.ensure_collections([4, 2])
    chunks = _fake_chunks(3, spec_id="38.401")
    w.upsert_multidim(chunks, {4: [[0.1, 0.2, 0.3, 0.4]] * 3, 2: [[0.6, 0.8]] * 3})
    # base 名（无 dim）未建
    assert not w._client.collection_exists(base_name)
    # 默认 purge 走主 dim（max=4）collection
    removed = w.purge_spec("38.401")
    assert removed == 3
    assert w.count(spec_id="38.401") == 0
    # 副 dim collection 没被动（caller 用 _list_provider_collections 自行遍历）
    assert w.count(spec_id="38.401", collection_name="tpurge_main_voyage_d2") == 3


def test_purge_spec_explicit_collection_name_overrides() -> None:
    """purge_spec(collection_name=...) 显式覆盖：可指定清任意 dim collection。"""
    w = _md_writer("tpurge_explicit")
    w.ensure_collections([4, 2])
    chunks = _fake_chunks(2, spec_id="38.331")
    w.upsert_multidim(chunks, {4: [[1.0, 0, 0, 0]] * 2, 2: [[1.0, 0]] * 2})
    # 显式清 d2 collection
    removed = w.purge_spec("38.331", collection_name="tpurge_explicit_voyage_d2")
    assert removed == 2
    assert w.count(spec_id="38.331", collection_name="tpurge_explicit_voyage_d2") == 0
    # d4 没被动
    assert w.count(spec_id="38.331", collection_name="tpurge_explicit_voyage_d4") == 2
    # 不存在的 collection_name 返回 0 不抛
    assert w.purge_spec("38.331", collection_name="never_created") == 0


def test_purge_spec_falls_back_to_self_collection_name_in_single_dim_mode() -> None:
    """单 dim 路径保持原行为：purge 走 self.collection_name。"""
    w = QdrantWriter(
        client=QdrantClient(":memory:"),
        provider="voyage",
        dim=4,
        collection_name="single_dim_purge_test",
    )
    w.ensure_collection()
    chunks = _fake_chunks(2, spec_id="29.503")
    w.upsert_chunks(chunks, [[0.1, 0.2, 0.3, 0.4]] * 2)
    assert w._collections_by_dim == {}
    removed = w.purge_spec("29.503")
    assert removed == 2
    assert w.count(spec_id="29.503") == 0
