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


async def test_params_tool_filtered_when_llm_drifts() -> None:
    """2026-05-28 防御性兜底：即使 LLM 漏改 v2 prompt 仍产 `params`，classify_node
    必须把 LLM 自动产的 params 过滤掉（避免 "DCI 1_1 字段 → BM25 dump" 回归）。"""
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "query_class": "tool",
                    "complexity": "simple",
                    "detected_language": "zh",
                    "rewritten_query": "DCI format 1_1 field list",
                    "needs_explicit_tools": ["params"],
                    "reason": "v1 prompt drift",
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    state = AgentState(user_input="列出 DCI1_1 字段")

    out = await classify_node(state, deps=deps)
    # LLM 自动产的 params 被过滤掉
    assert "params" not in out["explicit_tools"]
    # 所有 tool 意图都被过滤 → query_class 降级成 definition，让查询走 RAG
    assert out["query_class"] == "definition"


async def test_user_explicit_params_in_state_still_honored() -> None:
    """用户在前端显式勾 params（写到 state.explicit_tools）是主动意图，要保留。"""
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "query_class": "definition",
                    "complexity": "simple",
                    "detected_language": "en",
                    "rewritten_query": "where does field X appear",
                    "needs_explicit_tools": [],
                    "reason": "user invoked tool explicitly",
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    state = AgentState(user_input="where does field foo appear", explicit_tools=["params"])

    out = await classify_node(state, deps=deps)
    assert "params" in out["explicit_tools"]
    # query_class 由 LLM 自己决定，不强制改


async def test_mixed_tools_with_params_only_filters_params() -> None:
    """LLM 同时给了 glossary + params：保留 glossary，去掉 params，仍走 tool 路径。"""
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "query_class": "tool",
                    "complexity": "simple",
                    "detected_language": "en",
                    "rewritten_query": "abbreviations starting with S",
                    "needs_explicit_tools": ["glossary", "params"],
                    "reason": "mixed",
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    state = AgentState(user_input="缩写表里以 S 开头的")

    out = await classify_node(state, deps=deps)
    assert out["explicit_tools"] == ["glossary"]
    assert out["query_class"] == "tool"


async def test_classify_disables_thinking_for_determinism() -> None:
    """回归：mimo 思考模式下 temperature=0 被强制 1.0 → 同题分类（simple/complex/
    query_class）会跳变让路由不稳；reasoning 还偶发吃光预算返空 JSON 走 _FALLBACK。
    thinking=disabled 后分类完全确定性。"""
    from app.agent.nodes import classify_node
    from app.agent.state import AgentState

    from .conftest import StubLLM, make_deps

    payload = '{"query_class":"procedure","complexity":"simple","rewritten_query":"q"}'
    llm = StubLLM(responses=[payload])
    deps = make_deps(llm=llm)
    await classify_node(AgentState(user_input="q"), deps=deps)
    chat = next(c for c in llm.calls if c["kind"] == "chat")
    assert chat["thinking"] == {"type": "disabled"}
