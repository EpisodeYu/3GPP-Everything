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
