"""multi_query_node 单测（M4.3 complex 分支）。"""

from __future__ import annotations

import json

from app.agent.nodes import multi_query_node
from app.agent.state import AgentState
from app.core.errors import LLMError

from .conftest import StubLLM, make_deps


async def test_multi_query_parses_json_array_and_keeps_primary_first() -> None:
    sub = ["AMF definition in 3GPP", "AMF signalling N1 interface", "AMF role in registration"]
    llm = StubLLM(responses=[json.dumps(sub)])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="What is AMF?", rewritten_queries=["AMF function definition"])
    out = await multi_query_node(state, deps=deps)
    queries = out["rewritten_queries"]
    assert queries[0] == "AMF function definition"
    assert set(queries[1:]) == set(sub)


async def test_multi_query_dedupes_against_primary_case_insensitive() -> None:
    sub = ["AMF function definition", "AMF role"]  # 第一条与 primary 同
    llm = StubLLM(responses=[json.dumps(sub)])
    deps = make_deps(llm=llm)
    state = AgentState(rewritten_queries=["AMF function definition"])
    out = await multi_query_node(state, deps=deps)
    assert out["rewritten_queries"] == ["AMF function definition", "AMF role"]


async def test_multi_query_falls_back_when_llm_returns_garbage() -> None:
    llm = StubLLM(responses=["sorry I cannot do that"])
    deps = make_deps(llm=llm)
    state = AgentState(rewritten_queries=["original query"])
    out = await multi_query_node(state, deps=deps)
    # garbage 文本按 line 拆，至少保留原 query
    queries = out["rewritten_queries"]
    assert queries[0] == "original query"


async def test_multi_query_llm_failure_preserves_original_queries() -> None:
    class FailingLLM(StubLLM):
        async def chat(self, *args, **kwargs):  # type: ignore[override]
            raise LLMError("boom")

    deps = make_deps(llm=FailingLLM(responses=[]))
    state = AgentState(rewritten_queries=["original"])
    out = await multi_query_node(state, deps=deps)
    assert out["rewritten_queries"] == ["original"]


async def test_multi_query_caps_at_six_total() -> None:
    sub = [f"sub query {i}" for i in range(10)]
    llm = StubLLM(responses=[json.dumps(sub)])
    deps = make_deps(llm=llm)
    state = AgentState(rewritten_queries=["primary"])
    out = await multi_query_node(state, deps=deps)
    assert len(out["rewritten_queries"]) == 1 + 5  # primary + 最多 5 个 sub


async def test_multi_query_extracts_array_from_prose() -> None:
    text = 'Here are some queries: ["q1", "q2", "q3"] hope that helps.'
    llm = StubLLM(responses=[text])
    deps = make_deps(llm=llm)
    state = AgentState(rewritten_queries=["primary"])
    out = await multi_query_node(state, deps=deps)
    assert out["rewritten_queries"] == ["primary", "q1", "q2", "q3"]


async def test_multi_query_passes_enough_max_tokens_for_reasoning_model() -> None:
    """回归：LIGHT 模型是 reasoning model；早期 max_tokens=400 在复杂查询上被
    reasoning 吃满（实测 reasoning=399 content=''），complex 链路退化为单 query
    检索。锁住下限避免回退。"""
    llm = StubLLM(responses=['["a","b","c"]'])
    deps = make_deps(llm=llm)
    state = AgentState(rewritten_queries=["primary"])
    await multi_query_node(state, deps=deps)
    chat = next(c for c in llm.calls if c["kind"] == "chat")
    assert chat["max_tokens"] >= 2048
