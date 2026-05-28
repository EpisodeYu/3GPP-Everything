"""generate_node：调 LLM 拼最终答案，并用正则抽 citations。"""

from __future__ import annotations

from app.agent.nodes import generate_node, parse_citations
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk

from .conftest import StubLLM, make_deps


def _chunk(cid: str, *, spec: str, section: tuple[str, ...]) -> StateChunk:
    return StateChunk(
        chunk_id=cid,
        spec_id=spec,
        section_path=section,
        section_title=" / ".join(section),
        chunk_type="text",
        content=f"chunk content {cid}",
        score_rerank=0.9,
    )


async def test_generate_streams_answer_and_extracts_citations() -> None:
    answer = (
        "AMF is the Access and Mobility Management Function "
        "[23.501 §6.3.1]. It anchors NAS signalling [23.501 §6.3.1]."
    )
    llm = StubLLM(responses=[answer])
    deps = make_deps(llm=llm)
    state = AgentState(
        user_input="What is AMF?",
        user_language="en",
        rewritten_queries=["AMF function definition"],
        reranked=[
            _chunk("c1", spec="23.501", section=("6", "3", "1")),
            _chunk("c2", spec="38.331", section=("5", "3")),
        ],
    )

    out = await generate_node(state, deps=deps)
    assert "AMF" in out["final_answer"]
    assert len(out["citations"]) == 1, "重复引用应去重"
    cite = out["citations"][0]
    assert cite["spec_id"] == "23.501"
    assert cite["chunk_id"] == "c1"
    assert cite["section_path"] == "6.3.1"
    # 生产路径走 chat_stream（不是非流式 chat）
    stream_calls = [c for c in llm.calls if c["kind"] == "chat_stream"]
    assert len(stream_calls) == 1
    assert stream_calls[0]["model"] == deps.settings.LLM_AGENT_MODEL
    # 不应再回落到非流式 chat
    assert not [c for c in llm.calls if c["kind"] == "chat"]


async def test_generate_falls_back_to_nonstream_when_stream_fails() -> None:
    """流式抛 LLMError → 回退到非流式 chat() 再产答案。"""
    from collections.abc import Sequence
    from typing import Any

    from app.core.errors import LLMError

    from .conftest import StubLLM as _Stub

    class _StreamBoomLLM(_Stub):
        async def chat_stream(  # type: ignore[override]
            self, messages: Sequence[dict[str, Any]], **kwargs: Any
        ) -> Any:
            self.calls.append({"kind": "chat_stream", "messages": list(messages), **kwargs})
            raise LLMError("network down")
            yield  # 让 mypy 知道这是 async generator

    llm = _StreamBoomLLM(responses=["Fallback answer [23.501 §6.3.1]."])
    deps = make_deps(llm=llm)
    state = AgentState(
        user_input="X",
        user_language="en",
        reranked=[_chunk("c1", spec="23.501", section=("6", "3", "1"))],
    )
    out = await generate_node(state, deps=deps)
    assert "Fallback answer" in out["final_answer"]
    assert out["citations"][0]["chunk_id"] == "c1"
    assert [c["kind"] for c in llm.calls] == ["chat_stream", "chat"]


async def test_no_chunks_returns_fallback_message() -> None:
    deps = make_deps(llm=StubLLM(responses=["should not be called"]))
    state_en = AgentState(user_input="X", user_language="en", reranked=[])
    out_en = await generate_node(state_en, deps=deps)
    assert "Not found" in out_en["final_answer"]
    assert out_en["citations"] == []
    assert out_en["confidence"] == 0.0

    state_zh = AgentState(user_input="X", user_language="zh", reranked=[])
    out_zh = await generate_node(state_zh, deps=deps)
    assert "未在已索引" in out_zh["final_answer"]


def test_parse_citations_handles_section_prefix_match() -> None:
    chunks = [
        _chunk("c1", spec="38.331", section=("5", "3", "5", "1")),
    ]
    answer = "see [38.331 §5.3] for details."
    cites = parse_citations(answer, chunks)
    assert len(cites) == 1
    assert cites[0]["chunk_id"] == "c1"
    assert cites[0]["cite_section_path"] == "5.3"


def test_parse_citations_skips_unknown_spec() -> None:
    chunks = [_chunk("c1", spec="38.331", section=("5", "3"))]
    answer = "see [99.999 §1.2] which we do not have."
    assert parse_citations(answer, chunks) == []
