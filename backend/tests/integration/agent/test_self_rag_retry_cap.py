"""M4.3 self-RAG retry 上限：低置信度场景不会死循环。

口径：`docs/03-development/03-agent.md §4.8 + §14 M4.3 第 3 条`。
- StubLLM 让 classify=complex；hyde / multi_query / generate / self_rag 多轮排队
- self_rag 持续返回 verdict=retry
- graph 在 retry_count >= 2 时强制走 END，retrieve 节点最多被调用 3 次（首跑 + 两次 retry）
"""

from __future__ import annotations

import json

import pytest

from app.agent.graph import build_graph
from app.agent.state import AgentState

from ...unit.agent.conftest import (
    StubDense,
    StubLLM,
    StubReranker,
    StubSparse,
    make_chunk,
    make_deps,
)

pytestmark = pytest.mark.integration


def _retry_resp(missing: list[str]) -> str:
    return json.dumps(
        {
            "faithful": False,
            "coverage": 0.3,
            "confidence": 0.4,
            "verdict": "retry",
            "missing_aspects": missing,
        }
    )


async def test_self_rag_retry_cap_forces_convergence_after_two_retries() -> None:
    """self_rag_node 在 verdict=retry 时把 retry_count += 1；graph 条件边
    `retry_count < 2` 才允许回 retrieve。语义：retry_count 一旦达到 2 立即 END，
    确保哪怕 self_rag 永远说 retry 也不会死循环（即使是诡异输入也最多一次回环）。
    """
    chunk = make_chunk(
        "c1",
        spec_id="38.331",
        section=("5", "3", "5"),
        title="RRC",
        content="RRC connection setup. See also AMF interactions.",
    )
    classify_resp = json.dumps(
        {
            "query_class": "procedure",
            "complexity": "complex",
            "detected_language": "en",
            "rewritten_query": "RRC connection setup procedure",
            "needs_explicit_tools": [],
            "reason": "multi-entity",
        }
    )
    rewrite_resp = "RRC connection setup detailed steps"
    hyde_resp = "RRC connection setup involves SRB establishment ..."
    multi_query_resp = json.dumps(["RRC SRB setup", "RRC msg5 contents"])
    generate_resp = "RRC connection establishment ... [38.331 §5.3.5]"
    # 排队足够多 self_rag 应答：永远说 retry。实际只会被消费 2 次。
    self_rag_resps = [
        _retry_resp(["msg5 fields"]),
        _retry_resp(["NAS interaction"]),
        _retry_resp(["timer T300"]),  # 不应被消费 — 若被消费，说明没收敛
    ]

    llm = StubLLM(
        responses=[
            classify_resp,
            rewrite_resp,
            hyde_resp,
            multi_query_resp,
            generate_resp,
            self_rag_resps[0],
            generate_resp,
            self_rag_resps[1],
            generate_resp,
            self_rag_resps[2],
        ]
    )
    dense = StubDense(chunks=[chunk])
    sparse = StubSparse(chunks=[chunk])
    deps = make_deps(
        llm=llm,
        dense=dense,
        sparse=sparse,
        reranker=StubReranker(scores=[0.95]),
    )
    graph = build_graph(deps)

    out = await graph.ainvoke({"user_input": "How does RRC connection setup work?"})
    state = AgentState.model_validate(out)

    # 收敛断言：retry_count 锁在 2
    assert state.retry_count == 2, f"retry_count 应=2，实际={state.retry_count}"
    # verdict 最后一次仍是 retry（self_rag_node 不会改 verdict，graph 强制 END）
    assert state.self_rag_verdict == "retry"
    # 总 chat 调用 = classify + rewrite + hyde + multi_query + 2×(generate + self_rag) = 8
    chat_calls = [c for c in llm.calls if c["kind"] == "chat"]
    assert len(chat_calls) == 8, f"chat 调用应=8，实际={len(chat_calls)}"
    # missing_aspects 第三轮的内容（timer T300）不应进 rewritten_queries（第三次 self_rag 没被调）
    assert "timer T300" not in state.rewritten_queries
    # 前两轮的 missing 进了
    assert "msg5 fields" in state.rewritten_queries
    assert "NAS interaction" in state.rewritten_queries
