"""params_tool 单测：sparse → 过滤 chunk_type → 返回 hits。"""

from __future__ import annotations

from app.agent.state import AgentState
from app.tools.params import params_tool

from ..agent.conftest import StubSparse, make_chunk, make_deps


async def test_params_tool_empty_query_returns_warning() -> None:
    deps = make_deps()
    out = await params_tool(AgentState(user_input=""), deps=deps)
    assert out["warning"] == "empty query"
    assert out["hits"] == []


async def test_params_tool_no_sparse_returns_warning() -> None:
    deps = make_deps()  # sparse=None by default
    out = await params_tool(AgentState(user_input="rsrp threshold"), deps=deps)
    assert out["warning"] == "sparse retriever unavailable"
    assert out["hits"] == []


async def test_params_tool_filters_chunk_type_to_text_table() -> None:
    chunks = [
        make_chunk("c1", chunk_type="text"),
        make_chunk("c2", chunk_type="table"),
        make_chunk("c3", chunk_type="figure"),  # 应被过滤
        make_chunk("c4", chunk_type="formula"),  # 应被过滤
    ]
    for c in chunks:
        c.score_sparse = 0.5
    sparse = StubSparse(chunks=chunks)
    deps = make_deps(sparse=sparse)
    out = await params_tool(AgentState(user_input="rsrp threshold"), deps=deps)
    types = {h["chunk_type"] for h in out["hits"]}
    assert types <= {"text", "table"}
    ids = [h["chunk_id"] for h in out["hits"]]
    assert ids == ["c1", "c2"]
    assert out["warning"] is None


async def test_params_tool_uses_rewritten_query_first() -> None:
    sparse = StubSparse(chunks=[make_chunk("c1", chunk_type="text")])
    deps = make_deps(sparse=sparse)
    state = AgentState(user_input="raw input", rewritten_queries=["rewritten q"])
    await params_tool(state, deps=deps)
    assert sparse.calls[-1]["query"] == "rewritten q"
