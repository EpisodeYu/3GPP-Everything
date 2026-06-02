"""AgentState 字段契约（口径见 docs/03-development/03-agent.md §2）。"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from app.agent.state import AgentState, RetrievedChunk
from app.retrieval.models import RetrievedChunk as RetrievalChunk


def test_default_construction() -> None:
    s = AgentState()
    assert s.user_input == ""
    assert s.user_language == "en"
    assert s.mode == "qa"
    assert s.complexity == "simple"
    assert s.retry_count == 0
    assert s.candidates == []
    assert s.reranked == []
    assert s.cancelled is False
    assert s.paused is False


def test_messages_accept_basemessage() -> None:
    s = AgentState(messages=[HumanMessage(content="hi")])
    assert len(s.messages) == 1
    assert s.messages[0].content == "hi"


def test_history_default_empty_and_accepts_dicts() -> None:
    assert AgentState().history == []
    s = AgentState(
        history=[
            {"role": "user", "content": "What is PUCCH-Config?"},
            {"role": "assistant", "content": "It is an IE in 38.331 ..."},
        ]
    )
    assert len(s.history) == 2
    assert s.history[0]["role"] == "user"


def test_raw_history_and_session_id_defaults_and_accept() -> None:
    assert AgentState().raw_history == []
    assert AgentState().session_id is None
    s = AgentState(
        raw_history=[{"id": "abc", "role": "user", "content": "What is PUCCH-Config?"}],
        session_id="sess-1",
    )
    assert s.raw_history[0]["id"] == "abc"
    assert s.session_id == "sess-1"


def test_contextualized_input_default_empty() -> None:
    assert AgentState().contextualized_input == ""


def test_effective_query_prefers_contextualized_then_falls_back() -> None:
    # 首轮 / 未消解：回退原始 user_input
    assert AgentState(user_input="它的默认值?").effective_query == "它的默认值?"
    # 多轮已消解：优先用 contextualized_input
    s = AgentState(
        user_input="它的默认值?",
        contextualized_input="PUCCH-Config 的 maxNrofPUCCH-Resources 默认值是多少?",
    )
    assert s.effective_query == "PUCCH-Config 的 maxNrofPUCCH-Resources 默认值是多少?"


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        AgentState(unknown_field=42)  # type: ignore[call-arg]


def test_retrieved_chunk_from_retrieval_round_trip() -> None:
    src = RetrievalChunk(
        chunk_id="c1",
        spec_id="38.331",
        section_path=("5", "3", "5"),
        section_title="t",
        chunk_type="text",
        content="x" * 50,
        score_dense=0.91,
        score_sparse=0.42,
        score_rerank=0.88,
        fused_score=0.05,
        extra={"clause": "5.3.5"},
    )
    sc = RetrievedChunk.from_retrieval(src)
    assert sc.chunk_id == "c1"
    assert sc.section_path == ("5", "3", "5")
    assert sc.score_dense == 0.91
    assert sc.score_rerank == 0.88
    assert sc.fused_score == 0.05
    assert sc.extra == {"clause": "5.3.5"}


def test_retry_count_int() -> None:
    s = AgentState(retry_count=2)
    assert s.retry_count == 2
    with pytest.raises(ValidationError):
        AgentState(retry_count="x")  # type: ignore[arg-type]
