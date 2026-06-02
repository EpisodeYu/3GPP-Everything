"""build_simple_graph 端到端 smoke：所有节点 mock，校验状态流。"""

from __future__ import annotations

import json
import uuid

from app.agent.graph import _after_classify, _entry_router, build_graph, build_simple_graph
from app.agent.state import AgentState

from .conftest import StubDense, StubLLM, StubReranker, StubSparse, make_chunk, make_deps


class TestAfterClassifyRouting:
    """_after_classify 纯函数路由：tool / complex / simple + definition 走扩展链。"""

    def test_tool_class_routes_to_tool(self) -> None:
        st = AgentState(user_input="q", query_class="tool", complexity="simple")
        assert _after_classify(st) == "tool"

    def test_complex_routes_to_complex(self) -> None:
        st = AgentState(user_input="q", query_class="procedure", complexity="complex")
        assert _after_classify(st) == "complex"

    def test_plain_simple_routes_to_simple(self) -> None:
        st = AgentState(user_input="q", query_class="procedure", complexity="simple")
        assert _after_classify(st) == "simple"

    def test_definition_simple_routes_to_complex_for_expansion(self) -> None:
        # 定义题即便被判 simple，也应走 complex 扩展链（hyde+multi_query）提升召回。
        st = AgentState(user_input="q", query_class="definition", complexity="simple")
        assert _after_classify(st) == "complex"

    def test_tool_wins_over_definition(self) -> None:
        # query_class=tool 优先级最高，不被 definition 抢走。
        st = AgentState(user_input="q", query_class="tool", complexity="complex")
        assert _after_classify(st) == "tool"


class TestEntryRouter:
    """_entry_router：有 prior 历史（多轮）先走 compact_history；首轮直连 classify。"""

    def test_no_history_routes_to_classify(self) -> None:
        assert _entry_router(AgentState(user_input="q")) == "classify"

    def test_with_raw_history_routes_to_compact_history(self) -> None:
        st = AgentState(
            user_input="它的默认值?",
            raw_history=[
                {"id": str(uuid.uuid4()), "role": "user", "content": "What is PUCCH-Config?"}
            ],
        )
        assert _entry_router(st) == "compact_history"


async def test_multiturn_compacts_then_resolves_and_uses_query() -> None:
    """端到端（mock）：有 raw_history → compact_history → contextualize → 消解 query 进检索。"""
    resolved = "What is the default value of PUCCH-Config?"
    classify_resp = json.dumps(
        {
            "query_class": "procedure",
            "complexity": "simple",
            "detected_language": "en",
            "rewritten_query": "PUCCH-Config default value",
            "needs_explicit_tools": [],
            "reason": "follow-up",
        }
    )
    generate_resp = "The default value is 4 [1]."
    self_rag_resp = json.dumps(
        {
            "faithful": True,
            "coverage": 0.9,
            "confidence": 0.8,
            "verdict": "accept",
            "missing_aspects": [],
        }
    )
    # 短历史（<= 8）→ compact_history 不调 LLM；调用顺序：
    # contextualize(chat) → classify(chat) → generate(chat_stream) → self_rag(chat)
    llm = StubLLM(responses=[resolved, classify_resp, generate_resp, self_rag_resp])

    chunk = make_chunk("c1", spec_id="38.331", section=("5", "3", "5"), title="PUCCH-Config")
    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[chunk]),
        sparse=StubSparse(chunks=[chunk]),
        reranker=StubReranker(scores=[0.95]),
    )

    graph = build_graph(deps)
    out = await graph.ainvoke(
        {
            "user_input": "它的默认值是多少?",
            "user_language": "en",
            "session_id": "sess-1",
            "raw_history": [
                {"id": str(uuid.uuid4()), "role": "user", "content": "What is PUCCH-Config?"},
                {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": "PUCCH-Config is an IE in 38.331 [1].",
                },
            ],
        }
    )
    state = AgentState.model_validate(out)

    # compact_history 把 raw_history 压成 history（短历史 → 原文最近 N 条，只剩 role/content）
    assert len(state.history) == 2
    assert all(set(h.keys()) == {"role", "content"} for h in state.history)
    # contextualize 写入了消解后的自包含问题
    assert state.contextualized_input == resolved
    # classify 看到的是消解后的 query（第二次 chat 调用 = classify）
    chat_calls = [c for c in llm.calls if c["kind"] == "chat"]
    classify_prompt = chat_calls[1]["messages"][0]["content"]
    assert resolved in classify_prompt
    # 端到端拿到答案 + 引用
    assert "default value" in state.final_answer
    assert state.citations and state.citations[0]["spec_id"] == "38.331"


async def test_first_turn_no_history_skips_contextualize() -> None:
    """首轮无历史：不跑 compact_history / contextualize（无对应 LLM 调用），与单轮一致。"""
    classify_resp = json.dumps(
        {
            "query_class": "procedure",
            "complexity": "simple",
            "detected_language": "en",
            "rewritten_query": "AMF function",
            "needs_explicit_tools": [],
            "reason": "x",
        }
    )
    self_rag_resp = json.dumps(
        {
            "faithful": True,
            "coverage": 0.9,
            "confidence": 0.8,
            "verdict": "accept",
            "missing_aspects": [],
        }
    )
    llm = StubLLM(responses=[classify_resp, "AMF is ... [1].", self_rag_resp])
    chunk = make_chunk("c1", spec_id="23.501", section=("6", "3", "1"), title="AMF")
    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[chunk]),
        sparse=StubSparse(chunks=[chunk]),
        reranker=StubReranker(scores=[0.9]),
    )

    graph = build_graph(deps)
    out = await graph.ainvoke({"user_input": "What is AMF?", "user_language": "en"})
    state = AgentState.model_validate(out)

    assert state.contextualized_input == ""  # contextualize 未触发
    # 3 次 chat-like 调用：classify / generate / self_rag（无 contextualize 那次）
    chat_like = [c for c in llm.calls if c["kind"] in ("chat", "chat_stream")]
    assert len(chat_like) == 3
    assert state.final_answer


async def test_simple_path_runs_all_nodes_in_order() -> None:
    classify_resp = json.dumps(
        {
            "query_class": "definition",
            "complexity": "simple",
            "detected_language": "en",
            "rewritten_query": "AMF function definition",
            "needs_explicit_tools": [],
            "reason": "single term",
        }
    )
    generate_resp = "AMF is the Access and Mobility Management Function [1]."
    self_rag_resp = json.dumps(
        {
            "faithful": True,
            "coverage": 0.9,
            "confidence": 0.88,
            "verdict": "accept",
            "missing_aspects": [],
        }
    )
    llm = StubLLM(responses=[classify_resp, generate_resp, self_rag_resp])

    chunk = make_chunk(
        "c1",
        spec_id="23.501",
        section=("6", "3", "1"),
        title="AMF",
        content="AMF stands for Access and Mobility Management Function.",
    )
    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[chunk]),
        sparse=StubSparse(chunks=[chunk]),
        reranker=StubReranker(scores=[0.95]),
    )

    graph = build_simple_graph(deps)
    out = await graph.ainvoke({"user_input": "What is AMF?"})

    state = AgentState.model_validate(out)
    assert state.query_class == "definition"
    assert state.complexity == "simple"
    assert state.rewritten_queries == ["AMF function definition"]
    assert state.candidates, "retrieve 应该把 chunk 放到 candidates 里"
    assert state.reranked, "rerank 应该产出 reranked"
    assert state.reranked[0].chunk_id == "c1"
    assert "AMF" in state.final_answer
    assert state.citations and state.citations[0]["spec_id"] == "23.501"
    assert state.self_rag_verdict == "accept"
    assert state.confidence == 0.88

    # 三次 LLM 调用：classify(chat) / generate(chat_stream) / self_rag(chat)。
    # generate 节点用 chat_stream 真流式拉 token（口径 §4.7）；classify / self_rag
    # 仍走非流式 chat。
    llm_calls = [c for c in llm.calls if c["kind"] in ("chat", "chat_stream")]
    assert len(llm_calls) == 3
    assert [c["kind"] for c in llm_calls] == ["chat", "chat_stream", "chat"]
    # 模型路由：classify+self_rag 用 light，generate 用 agent
    assert llm_calls[0]["model"] == deps.settings.LLM_LIGHT_MODEL
    assert llm_calls[1]["model"] == deps.settings.LLM_AGENT_MODEL
    assert llm_calls[2]["model"] == deps.settings.LLM_LIGHT_MODEL


async def test_simple_path_no_chunks_yields_fallback_answer() -> None:
    classify_resp = json.dumps(
        {
            "query_class": "definition",
            "complexity": "simple",
            "detected_language": "en",
            "rewritten_query": "obscure term",
            "needs_explicit_tools": [],
            "reason": "x",
        }
    )
    llm = StubLLM(responses=[classify_resp])
    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[]),
        sparse=StubSparse(chunks=[]),
        reranker=StubReranker(scores=[]),
    )

    graph = build_simple_graph(deps)
    out = await graph.ainvoke({"user_input": "totally unknown thing"})
    state = AgentState.model_validate(out)
    assert state.final_answer.startswith("Not found")
    assert state.citations == []
    assert state.self_rag_verdict == "accept"
    # generate / self_rag 都没真正 call LLM（reranked 空 → fallback）；只 classify 调了一次
    chat_calls = [c for c in llm.calls if c["kind"] == "chat"]
    assert len(chat_calls) == 1
