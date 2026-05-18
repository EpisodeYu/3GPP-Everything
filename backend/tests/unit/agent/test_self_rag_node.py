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
        final_answer="AMF stands for Access and Mobility Management Function [23.501 §6.3.1].",
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


# === Batch B.1 / R8 + O3 · citation 真实性核对 ===


def _state_with_partial_hallucinated_citations() -> AgentState:
    """answer 引 2 个 citation：1 个落在 reranked 集合内，1 个无对应 chunk → hit_rate=0.5。"""
    return AgentState(
        user_input="AMF + SMF?",
        final_answer=("AMF is described in [23.501 §6.3.1] and SMF in [23.501 §6.3.2]."),
        reranked=[
            StateChunk(
                chunk_id="c1",
                spec_id="23.501",
                section_path=("6", "3", "1"),
                section_title="AMF",
                chunk_type="text",
                content="AMF stuff.",
            )
        ],
    )


def _state_with_mostly_hallucinated_citations() -> AgentState:
    """answer 引 3 个 citation：全部 spec_id 不在 reranked → hit_rate=0.0。"""
    return AgentState(
        user_input="weird question",
        final_answer=("See [38.331 §5.3], [38.413 §8.2.1], and [29.518 §5.4]."),
        reranked=[
            StateChunk(
                chunk_id="c1",
                spec_id="23.501",
                section_path=("6", "3", "1"),
                section_title="AMF",
                chunk_type="text",
                content="unrelated.",
            )
        ],
    )


async def test_partial_hallucinated_citation_dampens_confidence_but_keeps_verdict() -> None:
    llm = StubLLM(
        responses=[
            json.dumps(
                {
                    "faithful": True,
                    "coverage": 0.9,
                    "confidence": 0.8,
                    "verdict": "accept",
                    "missing_aspects": [],
                }
            )
        ]
    )
    deps = make_deps(llm=llm)
    out = await self_rag_node(_state_with_partial_hallucinated_citations(), deps=deps)
    assert out["self_rag_verdict"] == "accept"
    # hit_rate = 0.5 → confidence *= 0.5
    assert out["confidence"] == 0.4


async def test_mostly_hallucinated_citation_simple_path_forces_confidence_zero() -> None:
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
    out = await self_rag_node(
        _state_with_mostly_hallucinated_citations(), deps=deps, allow_retry=False
    )
    # simple path 不死循环：verdict 保留 accept，但 confidence=0
    assert out["self_rag_verdict"] == "accept"
    assert out["confidence"] == 0.0
    # simple path 不触发 retry 写 rewritten_queries
    assert "retry_count" not in out
    assert "rewritten_queries" not in out


async def test_mostly_hallucinated_citation_complex_path_forces_retry() -> None:
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
    out = await self_rag_node(
        _state_with_mostly_hallucinated_citations(), deps=deps, allow_retry=True
    )
    # complex path: hit_rate < 0.5 → 强制 retry，未命中 spec/section 进 missing
    assert out["self_rag_verdict"] == "retry"
    assert out["confidence"] == 0.0
    assert out["retry_count"] == 1
    queries = out["rewritten_queries"]
    # 三条 hallucinated citation 都应该作为 missing 拼回 queries
    assert any("38.331" in q for q in queries)
    assert any("38.413" in q for q in queries)
    assert any("29.518" in q for q in queries)
