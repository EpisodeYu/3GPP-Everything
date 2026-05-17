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
