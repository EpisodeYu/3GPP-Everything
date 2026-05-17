"""build_simple_graph 端到端 smoke：所有节点 mock，校验状态流。"""

from __future__ import annotations

import json

from app.agent.graph import build_simple_graph
from app.agent.state import AgentState

from .conftest import StubDense, StubLLM, StubReranker, StubSparse, make_chunk, make_deps


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
    generate_resp = "AMF is the Access and Mobility Management Function [23.501 §6.3.1]."
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

    # 三次 LLM 调用：classify / generate / self_rag
    chat_calls = [c for c in llm.calls if c["kind"] == "chat"]
    assert len(chat_calls) == 3
    # 模型路由：classify+self_rag 用 light，generate 用 agent
    assert chat_calls[0]["model"] == deps.settings.LLM_LIGHT_MODEL
    assert chat_calls[1]["model"] == deps.settings.LLM_AGENT_MODEL
    assert chat_calls[2]["model"] == deps.settings.LLM_LIGHT_MODEL


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
