"""retrieve_node：dense + sparse + RRF + 缓存路径 + map-reduce 分支。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.agent.nodes import retrieve_node
from app.agent.state import AgentState
from app.retrieval.models import RetrievedChunk as RetrievalChunk

from .conftest import StubDense, StubSparse, make_chunk, make_deps, make_settings


@dataclass
class QueryAwareDense:
    """按 query 返回不同 chunk 的 dense stub（map-reduce 测试用）。"""

    by_query: dict[str, list[RetrievalChunk]]
    calls: list[dict] = field(default_factory=list)

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 30,
        filter_spec_ids: Sequence[str] | None = None,
    ) -> list[RetrievalChunk]:
        self.calls.append({"query": query, "top_k": top_k})
        return list(self.by_query.get(query, []))[:top_k]

    async def close(self) -> None:
        pass


@dataclass
class QueryAwareSparse:
    by_query: dict[str, list[RetrievalChunk]]
    calls: list[dict] = field(default_factory=list)

    def retrieve(self, query: str, *, top_k: int = 30) -> list[RetrievalChunk]:
        self.calls.append({"query": query, "top_k": top_k})
        return list(self.by_query.get(query, []))[:top_k]


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


# ---- map-reduce 检索分支 ----


def _mapreduce_state(**kw) -> AgentState:
    base = dict(
        user_input="q",
        rewritten_queries=["q1", "q2"],
        complexity="complex",
        query_class="procedure",
    )
    base.update(kw)
    return AgentState(**base)


async def test_mapreduce_builds_per_query_pools() -> None:
    dense = QueryAwareDense(
        {"q1": [make_chunk("d1a"), make_chunk("d1b")], "q2": [make_chunk("d2a")]}
    )
    sparse = QueryAwareSparse({"q1": [make_chunk("s1a")], "q2": [make_chunk("s2a")]})
    settings = make_settings(RETRIEVAL_MAPREDUCE_ENABLED=True)
    deps = make_deps(dense=dense, sparse=sparse, settings=settings)  # type: ignore[arg-type]

    out = await retrieve_node(_mapreduce_state(), deps=deps)

    facets = out["candidates_by_query"]
    assert len(facets) == 2
    assert {c.chunk_id for c in facets[0]} == {"d1a", "d1b", "s1a"}
    assert {c.chunk_id for c in facets[1]} == {"d2a", "s2a"}
    # flat candidates 含全部 facet 的并集
    assert {c.chunk_id for c in out["candidates"]} == {"d1a", "d1b", "s1a", "d2a", "s2a"}


async def test_mapreduce_hyde_is_not_a_facet() -> None:
    dense = QueryAwareDense(
        {
            "q1": [make_chunk("d1a")],
            "q2": [make_chunk("d2a")],
            "hyde text": [make_chunk("hy")],
        }
    )
    settings = make_settings(RETRIEVAL_MAPREDUCE_ENABLED=True)
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]
    state = _mapreduce_state(hyde_doc="hyde text")

    out = await retrieve_node(state, deps=deps)

    # hyde 不单独成 facet：仍只 2 个 facet
    assert len(out["candidates_by_query"]) == 2
    # 但 hyde 命中进了 flat 池
    assert "hy" in {c.chunk_id for c in out["candidates"]}


async def test_mapreduce_disabled_uses_single_pool() -> None:
    dense = QueryAwareDense({"q1": [make_chunk("d1a")], "q2": [make_chunk("d2a")]})
    settings = make_settings(RETRIEVAL_MAPREDUCE_ENABLED=False)
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]

    out = await retrieve_node(_mapreduce_state(), deps=deps)
    assert "candidates_by_query" not in out  # single-pool 路径不写该 key


async def test_mapreduce_excluded_for_definition() -> None:
    dense = QueryAwareDense({"q1": [make_chunk("d1a")], "q2": [make_chunk("d2a")]})
    settings = make_settings(RETRIEVAL_MAPREDUCE_ENABLED=True)
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]

    out = await retrieve_node(_mapreduce_state(query_class="definition"), deps=deps)
    assert "candidates_by_query" not in out


async def test_mapreduce_excluded_for_simple() -> None:
    dense = QueryAwareDense({"q1": [make_chunk("d1a")], "q2": [make_chunk("d2a")]})
    settings = make_settings(RETRIEVAL_MAPREDUCE_ENABLED=True)
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]

    out = await retrieve_node(_mapreduce_state(complexity="simple"), deps=deps)
    assert "candidates_by_query" not in out


async def test_mapreduce_excluded_for_single_query() -> None:
    dense = QueryAwareDense({"only": [make_chunk("d1a")]})
    settings = make_settings(RETRIEVAL_MAPREDUCE_ENABLED=True)
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]

    out = await retrieve_node(_mapreduce_state(rewritten_queries=["only"]), deps=deps)
    assert "candidates_by_query" not in out
