"""retrieve_node：dense + sparse + RRF + 缓存路径。"""

from __future__ import annotations

import time

from app.agent.nodes import retrieve_node
from app.agent.state import AgentState

from .conftest import StubDense, StubSparse, make_chunk, make_deps


async def test_uses_rewritten_query_first() -> None:
    dense = StubDense(chunks=[make_chunk("c1", spec_id="38.331", section=("5", "3"))])
    sparse = StubSparse(chunks=[make_chunk("c2", spec_id="23.501", section=("6",))])
    deps = make_deps(dense=dense, sparse=sparse)
    state = AgentState(user_input="zh raw", rewritten_queries=["english version"])

    out = await retrieve_node(state, deps=deps)
    assert dense.calls[0]["query"] == "english version"
    assert sparse.calls[0]["query"] == "english version"
    cids = [c.chunk_id for c in out["candidates"]]
    assert set(cids) == {"c1", "c2"}


async def test_dedup_and_rrf_merge() -> None:
    common = make_chunk("c-shared", spec_id="38.331")
    dense = StubDense(chunks=[common, make_chunk("c-dense-only")])
    sparse = StubSparse(chunks=[common, make_chunk("c-sparse-only")])
    deps = make_deps(dense=dense, sparse=sparse)
    state = AgentState(user_input="q", rewritten_queries=["q"])

    out = await retrieve_node(state, deps=deps)
    cands = out["candidates"]
    cids = [c.chunk_id for c in cands]
    # 排重：c-shared 只出现一次
    assert cids.count("c-shared") == 1
    # 但融合分高于另两条（dense rank=1 + sparse rank=1）
    by_id = {c.chunk_id: c for c in cands}
    assert by_id["c-shared"].fused_score > by_id["c-dense-only"].fused_score
    assert by_id["c-shared"].fused_score > by_id["c-sparse-only"].fused_score


async def test_top_n_truncated_by_settings() -> None:
    # 12 条 dense + 12 条 sparse；settings.RETRIEVAL_FINAL_TOP_K = 20，但同一条的
    # chunk_id 排重不会比 20 多
    dense = StubDense(chunks=[make_chunk(f"d{i}") for i in range(12)])
    sparse = StubSparse(chunks=[make_chunk(f"s{i}") for i in range(12)])
    deps = make_deps(dense=dense, sparse=sparse)
    state = AgentState(user_input="q", rewritten_queries=["q"])

    out = await retrieve_node(state, deps=deps)
    assert len(out["candidates"]) == 20  # 12 + 8（被 top_n 截断）


async def test_user_input_used_when_no_rewritten() -> None:
    dense = StubDense(chunks=[make_chunk("c1")])
    deps = make_deps(dense=dense)
    state = AgentState(user_input="raw query")

    out = await retrieve_node(state, deps=deps)
    assert dense.calls[0]["query"] == "raw query"
    assert len(out["candidates"]) == 1


async def test_no_query_returns_empty() -> None:
    deps = make_deps(dense=StubDense(chunks=[make_chunk("c1")]))
    state = AgentState()
    out = await retrieve_node(state, deps=deps)
    assert out["candidates"] == []


async def test_retrieve_overhead_under_50ms_with_stubs() -> None:
    """retrieve 节点本身（不含真实 IO）应当几乎无开销。

    生产环境的 P50 ≤ 800ms 守约由 integration smoke 校验；这里只检查节点 wrapper
    的额外开销控制（rrf 融合 + cache 序列化），上限给宽松一点 50ms。
    """
    dense = StubDense(chunks=[make_chunk(f"d{i}") for i in range(30)])
    sparse = StubSparse(chunks=[make_chunk(f"s{i}") for i in range(30)])
    deps = make_deps(dense=dense, sparse=sparse)
    state = AgentState(user_input="q", rewritten_queries=["q"])

    t0 = time.perf_counter()
    await retrieve_node(state, deps=deps)
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed < 50.0, f"retrieve_node wrapper overhead {elapsed:.1f}ms exceeds 50ms"
