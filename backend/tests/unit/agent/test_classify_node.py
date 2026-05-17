"""classify_node 行为：按 LLM JSON 输出更新 query_class / complexity / rewritten_queries。"""

from __future__ import annotations

import json

from app.agent.nodes import classify_node
from app.agent.state import AgentState

from .conftest import StubLLM, make_deps


async def test_simple_definition_routes_to_simple_path() -> None:
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "query_class": "definition",
                    "complexity": "simple",
                    "detected_language": "en",
                    "rewritten_query": "AMF function definition",
                    "needs_explicit_tools": [],
                    "reason": "single term",
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    state = AgentState(user_input="What is AMF?")

    out = await classify_node(state, deps=deps)

    assert out["query_class"] == "definition"
    assert out["complexity"] == "simple"
    assert out["rewritten_queries"] == ["AMF function definition"]
    assert out["user_language"] == "en"
    assert out["explicit_tools"] == []
    assert llm.calls[0]["model"] == deps.settings.LLM_LIGHT_MODEL


async def test_chinese_input_translated_to_english_query() -> None:
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "query_class": "definition",
                    "complexity": "simple",
                    "detected_language": "zh",
                    "rewritten_query": "5G AMF role and functions",
                    "needs_explicit_tools": [],
                    "reason": "zh -> en",
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    state = AgentState(user_input="AMF 是什么")

    out = await classify_node(state, deps=deps)
    assert out["user_language"] == "zh"
    assert out["rewritten_queries"] == ["5G AMF role and functions"]


async def test_explicit_tool_request_propagates() -> None:
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "query_class": "tool",
                    "complexity": "simple",
                    "detected_language": "zh",
                    "rewritten_query": "latest 38.331 release status",
                    "needs_explicit_tools": ["web_search"],
                    "reason": "user asked to search",
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    state = AgentState(user_input="搜一下 38.331 最新版本进度", explicit_tools=["other"])

    out = await classify_node(state, deps=deps)
    assert "web_search" in out["explicit_tools"]
    assert "other" in out["explicit_tools"]


async def test_invalid_llm_response_falls_back() -> None:
    llm = StubLLM(responses=["this is not json"])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="anything")

    out = await classify_node(state, deps=deps)
    assert out["query_class"] == "unknown"
    assert out["rewritten_queries"] == ["anything"]


async def test_empty_input_short_circuits() -> None:
    llm = StubLLM(responses=[json.dumps({})])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="   ")
    out = await classify_node(state, deps=deps)
    assert out["query_class"] == "unknown"
    assert out["rewritten_queries"] == []
    # LLM 不应该被调用
    assert all(c["kind"] != "chat" for c in llm.calls)
