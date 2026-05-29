"""self_rag_node：grounding-only。M4.2 不进 retry，verdict 强制 accept。"""

from __future__ import annotations

import json

from app.agent.nodes import self_rag_node
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk

from .conftest import StubLLM, make_deps


def _state_with_answer() -> AgentState:
    return AgentState(
        user_input="What is AMF?",
        final_answer="AMF stands for Access and Mobility Management Function [1].",
        reranked=[
            StateChunk(
                chunk_id="c1",
                spec_id="23.501",
                section_path=("6", "3", "1"),
                section_title="AMF",
                chunk_type="text",
                content="AMF stands for Access and Mobility Management Function.",
            )
        ],
    )


async def test_accept_verdict_passes_through() -> None:
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "faithful": True,
                    "coverage": 0.9,
                    "confidence": 0.85,
                    "verdict": "accept",
                    "missing_aspects": [],
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    out = await self_rag_node(_state_with_answer(), deps=deps)
    assert out["self_rag_verdict"] == "accept"
    assert out["confidence"] == 0.85


async def test_retry_or_insufficient_forced_to_accept_when_allow_retry_false() -> None:
    for verdict in ("retry", "insufficient"):
        llm = StubLLM(
            responses=[
                json.dumps(
                    {
                        "faithful": False,
                        "coverage": 0.2,
                        "confidence": 0.3,
                        "verdict": verdict,
                        "missing_aspects": ["procedure steps"],
                    }
                )
            ]
        )
        deps = make_deps(llm=llm)
        out = await self_rag_node(_state_with_answer(), deps=deps, allow_retry=False)
        assert out["self_rag_verdict"] == "accept"
        assert out["self_rag_missing"] == ["procedure steps"]
        assert out["confidence"] == 0.3


async def test_retry_preserved_when_allow_retry_true() -> None:
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "faithful": True,
                    "coverage": 0.4,
                    "confidence": 0.5,
                    "verdict": "retry",
                    "missing_aspects": ["x"],
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    out = await self_rag_node(_state_with_answer(), deps=deps, allow_retry=True)
    assert out["self_rag_verdict"] == "retry"
    assert out["self_rag_missing"] == ["x"]


async def test_retry_increments_count_and_appends_missing_to_queries() -> None:
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "faithful": False,
                    "coverage": 0.3,
                    "confidence": 0.4,
                    "verdict": "retry",
                    "missing_aspects": ["AMF restart procedure", "N1 retransmission"],
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    state = _state_with_answer()
    state = state.model_copy(update={"rewritten_queries": ["AMF role"], "retry_count": 0})
    out = await self_rag_node(state, deps=deps, allow_retry=True)
    assert out["self_rag_verdict"] == "retry"
    assert out["retry_count"] == 1
    assert out["rewritten_queries"] == [
        "AMF role",
        "AMF restart procedure",
        "N1 retransmission",
    ]


async def test_accept_under_allow_retry_does_not_touch_queries() -> None:
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "faithful": True,
                    "coverage": 0.9,
                    "confidence": 0.9,
                    "verdict": "accept",
                    "missing_aspects": [],
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    state = _state_with_answer().model_copy(update={"rewritten_queries": ["q1"]})
    out = await self_rag_node(state, deps=deps, allow_retry=True)
    assert out["self_rag_verdict"] == "accept"
    assert "retry_count" not in out
    assert "rewritten_queries" not in out


async def test_no_answer_short_circuits() -> None:
    deps = make_deps(llm=StubLLM(responses=["should not be called"]))
    state = AgentState(user_input="x", final_answer="")
    out = await self_rag_node(state, deps=deps)
    assert out["self_rag_verdict"] == "accept"
    assert out["confidence"] == 0.0


async def test_invalid_json_falls_back_to_accept_low_confidence() -> None:
    llm = StubLLM(responses=["not json"])
    deps = make_deps(llm=llm)
    out = await self_rag_node(_state_with_answer(), deps=deps)
    assert out["self_rag_verdict"] == "accept"
    assert out["confidence"] == 0.0


# v6 切到索引引用后 `_citation_hit_rate` 删除，下列原 R8+O3 case 一并退役：
#   - test_partial_hallucinated_citation_dampens_confidence_but_keeps_verdict
#   - test_mostly_hallucinated_citation_simple_path_forces_confidence_zero
#   - test_mostly_hallucinated_citation_complex_path_forces_retry
# 索引方案下 `parse_citations` 对越界 N 已直接 drop，进入 self_rag 的 citation
# 全部落在 reranked 集合，hit_rate 恒为 1.0，原检测面归零。grounding 真实性由
# LLM `faithful` + `coverage` 全权判定，相关 case 见 test_accept_verdict_passes_through
# 与 test_retry_*_when_allow_retry_*。


async def test_self_rag_disables_thinking_for_determinism() -> None:
    """回归：self_rag 是 verdict JSON 固定结构输出，思考模式 temp=0 被强制 1.0
    会让同样事实/回答偶发返不同 verdict（accept ↔ retry 跳变），retry 路径不稳。
    thinking=disabled 后 verdict 完全确定。"""
    from app.agent.nodes import self_rag_node
    from app.agent.state import AgentState, RetrievedChunk

    from .conftest import StubLLM, make_deps

    payload = (
        '{"faithful":true,"coverage":0.9,"confidence":0.9,'
        '"verdict":"accept","missing_aspects":[]}'
    )
    llm = StubLLM(responses=[payload])
    deps = make_deps(llm=llm)
    chunk = RetrievedChunk(
        chunk_id="c1",
        spec_id="38.331",
        section_path=("5", "3"),
        section_title="t",
        chunk_type="text",
        content="x",
    )
    state = AgentState(
        user_input="q",
        final_answer="a",
        reranked=[chunk],
        citations=[{"chunk_id": "c1", "spec_id": "38.331", "section_path": ["5", "3"]}],
    )
    await self_rag_node(state, deps=deps)
    chat = next(c for c in llm.calls if c["kind"] == "chat")
    assert chat["thinking"] == {"type": "disabled"}
