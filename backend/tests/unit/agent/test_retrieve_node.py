"""retrieve_node：dense + sparse + RRF + 缓存路径 + map-reduce 分支。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agent.nodes import retrieve_node
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk
from app.core.errors import RetrievalError
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


@dataclass
class TrackingDense:
    """记录在途调用数，并可让 query 乱序完成或按 query 注入失败。"""

    by_query: dict[str, list[RetrievalChunk]]
    delays: dict[str, float] = field(default_factory=dict)
    fail_on: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    active: int = 0
    max_active: int = 0

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 30,
        filter_spec_ids: Sequence[str] | None = None,
    ) -> list[RetrievalChunk]:
        self.calls.append(query)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delays.get(query, 0.01))
            if query in self.fail_on:
                raise RetrievalError(f"dense failed for {query}")
            return list(self.by_query.get(query, []))[:top_k]
        finally:
            self.active -= 1

    async def close(self) -> None:
        pass


@dataclass
class StubCache:
    value: Any
    get_calls: list[tuple[str, Any]] = field(default_factory=list)
    set_calls: list[tuple[str, Any, Any]] = field(default_factory=list)

    async def get(self, namespace: str, payload: Any) -> Any:
        self.get_calls.append((namespace, payload))
        return self.value

    async def set(self, namespace: str, payload: Any, value: Any) -> None:
        self.set_calls.append((namespace, payload, value))


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


async def test_single_pool_multi_query_uses_bounded_concurrency() -> None:
    queries = ["q1", "q2", "q3"]
    dense = TrackingDense({q: [make_chunk(f"d-{q}")] for q in queries})
    settings = make_settings(RETRIEVAL_QUERY_CONCURRENCY=2)
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]
    state = AgentState(user_input="q", rewritten_queries=queries)

    out = await retrieve_node(state, deps=deps)

    assert dense.max_active == 2
    assert dense.calls == queries
    assert {c.chunk_id for c in out["candidates"]} == {"d-q1", "d-q2", "d-q3"}


async def test_query_concurrency_non_positive_falls_back_to_serial() -> None:
    queries = ["q1", "q2", "q3"]
    dense = TrackingDense({q: [make_chunk(f"d-{q}")] for q in queries})
    settings = make_settings(RETRIEVAL_QUERY_CONCURRENCY=0)
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]

    await retrieve_node(AgentState(user_input="q", rewritten_queries=queries), deps=deps)

    assert dense.max_active == 1
    assert dense.calls == queries


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
    dense = TrackingDense(
        {"q1": [make_chunk("d1a"), make_chunk("d1b")], "q2": [make_chunk("d2a")]},
        delays={"q1": 0.03, "q2": 0.01},
    )
    sparse = QueryAwareSparse({"q1": [make_chunk("s1a")], "q2": [make_chunk("s2a")]})
    settings = make_settings(
        RETRIEVAL_MAPREDUCE_ENABLED=True,
        RETRIEVAL_QUERY_CONCURRENCY=2,
    )
    deps = make_deps(dense=dense, sparse=sparse, settings=settings)  # type: ignore[arg-type]

    out = await retrieve_node(_mapreduce_state(), deps=deps)

    assert dense.max_active == 2
    facets = out["candidates_by_query"]
    assert len(facets) == 2
    # q2 先完成，但 gather 必须按输入 query 顺序返回。
    assert {c.chunk_id for c in facets[0]} == {"d1a", "d1b", "s1a"}
    assert {c.chunk_id for c in facets[1]} == {"d2a", "s2a"}
    # flat candidates 含全部 facet 的并集
    assert {c.chunk_id for c in out["candidates"]} == {"d1a", "d1b", "s1a", "d2a", "s2a"}


async def test_mapreduce_hyde_is_not_a_facet() -> None:
    dense = TrackingDense(
        {
            "q1": [make_chunk("d1a")],
            "q2": [make_chunk("d2a")],
            "hyde text": [make_chunk("hy")],
        }
    )
    settings = make_settings(
        RETRIEVAL_MAPREDUCE_ENABLED=True,
        RETRIEVAL_QUERY_CONCURRENCY=3,
    )
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]
    state = _mapreduce_state(hyde_doc="hyde text")

    out = await retrieve_node(state, deps=deps)

    # HyDE 与 facet 同批并发，但不写 candidates_by_query。
    assert dense.max_active == 3
    # hyde 不单独成 facet：仍只 2 个 facet
    assert len(out["candidates_by_query"]) == 2
    # 但 hyde 命中进了 flat 池
    assert "hy" in {c.chunk_id for c in out["candidates"]}


async def test_mapreduce_parallel_query_failure_isolated() -> None:
    dense = TrackingDense(
        {"q1": [make_chunk("d1")], "q2": [make_chunk("d2")]},
        fail_on={"q2"},
    )
    sparse = QueryAwareSparse({"q2": [make_chunk("s2")]})
    settings = make_settings(
        RETRIEVAL_MAPREDUCE_ENABLED=True,
        RETRIEVAL_QUERY_CONCURRENCY=2,
    )
    deps = make_deps(dense=dense, sparse=sparse, settings=settings)  # type: ignore[arg-type]

    out = await retrieve_node(_mapreduce_state(), deps=deps)

    assert dense.max_active == 2
    assert {c.chunk_id for c in out["candidates_by_query"][0]} == {"d1"}
    assert {c.chunk_id for c in out["candidates_by_query"][1]} == {"s2"}


async def test_mapreduce_cache_hit_skips_parallel_fetch() -> None:
    dense = TrackingDense({"q1": [make_chunk("unexpected")]})
    cached = StateChunk.from_retrieval(make_chunk("cached"))
    cache = StubCache(
        {
            "flat": [cached.model_dump(mode="json")],
            "by_query": [[cached.model_dump(mode="json")]],
        }
    )
    settings = make_settings(RETRIEVAL_MAPREDUCE_ENABLED=True)
    deps = make_deps(dense=dense, sparse=None, settings=settings)  # type: ignore[arg-type]
    deps.cache = cache  # type: ignore[assignment]

    out = await retrieve_node(_mapreduce_state(), deps=deps)

    assert dense.calls == []
    assert [c.chunk_id for c in out["candidates"]] == ["cached"]
    assert [c.chunk_id for c in out["candidates_by_query"][0]] == ["cached"]
    assert len(cache.get_calls) == 1
    assert cache.set_calls == []


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
