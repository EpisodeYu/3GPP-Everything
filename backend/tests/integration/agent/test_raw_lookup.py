"""M4.3 raw_lookup 模式：retrieve + rerank only，不调用任何生成 LLM。

口径：`docs/03-development/03-agent.md §4.10` + §14 M4.3 第 2 条。
断言：
- final_answer 保持空（raw_lookup 不生成自然语言答案）
- reranked 含至少 1 个 chunk
- StubLLM.chat 调用次数 = 0（classify / generate / self_rag 都不应被触发）
- embed 调用可发生（DenseRetriever 走 LLM client 拿向量；本测试用 StubDense，
  不会触发 embed，但若改成真实 dense 走 embedder，依然只是 embed 而非 chat）
"""

from __future__ import annotations

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


async def test_raw_lookup_skips_classify_and_generate() -> None:
    chunk = make_chunk(
        "c1",
        spec_id="38.331",
        section=("5", "3", "5"),
        title="RRC connection",
        content="RRC connection establishment procedure ...",
    )
    llm = StubLLM(responses=["should never be called"])
    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[chunk]),
        sparse=StubSparse(chunks=[chunk]),
        reranker=StubReranker(scores=[0.9]),
    )
    graph = build_graph(deps)

    out = await graph.ainvoke(
        {
            "user_input": "list RRC connection setup chunks",
            "mode": "raw_lookup",
        }
    )
    state = AgentState.model_validate(out)

    # 检索 + rerank 跑了；生成相关字段全空
    assert state.candidates, "raw_lookup 仍要 retrieve 出 candidates"
    assert state.reranked, "raw_lookup 仍要 rerank 出 top-K"
    assert state.reranked[0].chunk_id == "c1"
    assert state.final_answer == ""
    assert state.citations == []
    assert state.self_rag_verdict is None

    # 关键断言：任何 chat（generation）LLM 调用都不应发生
    chat_calls = [c for c in llm.calls if c["kind"] == "chat"]
    assert chat_calls == [], f"raw_lookup 触发了 chat LLM 调用: {chat_calls!r}"
