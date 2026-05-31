"""hyde_node 单测（M4.3 complex 分支 + 2026-05-31 字符级流式 reasoning）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.agent.nodes import hyde_node
from app.agent.state import AgentState
from app.core.errors import LLMError

from .conftest import StubLLM, make_deps


async def test_hyde_streams_and_sets_state_hyde_doc() -> None:
    """流式路径正常：chat_stream 返回的 chunks 拼接 == hyde_doc，且不回落到 chat()。"""
    fake_doc = (
        "The Access and Mobility Management Function (AMF) terminates the N1/N2 interfaces "
        "and handles registration, connection, mobility and access authentication for UEs ..."
    )
    llm = StubLLM(responses=[fake_doc])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="What is AMF?")
    out = await hyde_node(state, deps=deps)
    assert out["hyde_doc"] == fake_doc.strip()
    # 走 chat_stream，不回退到 chat
    stream_calls = [c for c in llm.calls if c["kind"] == "chat_stream"]
    assert len(stream_calls) == 1
    # 使用 agent / pro 模型（hyde 需要生成质量更高的伪文档）
    assert stream_calls[0]["model"] == deps.settings.LLM_AGENT_MODEL
    assert not [c for c in llm.calls if c["kind"] == "chat"]


async def test_hyde_empty_user_input_returns_none() -> None:
    llm = StubLLM(responses=["should never be used"])
    deps = make_deps(llm=llm)
    out = await hyde_node(AgentState(user_input=""), deps=deps)
    assert out == {"hyde_doc": None}
    assert llm.calls == []  # 空输入不应调 LLM


async def test_hyde_falls_back_to_nonstream_when_stream_fails() -> None:
    """流式抛 LLMError → 回退非流式 chat() 拿整段，仍返回 hyde_doc。"""

    class _StreamBoomLLM(StubLLM):
        async def chat_stream(  # type: ignore[override]
            self, messages: Sequence[dict[str, Any]], **kwargs: Any
        ) -> Any:
            self.calls.append({"kind": "chat_stream", "messages": list(messages), **kwargs})
            raise LLMError("network down")
            yield  # async generator marker

    llm = _StreamBoomLLM(responses=["Fallback hyde doc body about AMF."])
    deps = make_deps(llm=llm)
    out = await hyde_node(AgentState(user_input="What is AMF?"), deps=deps)
    assert out["hyde_doc"] == "Fallback hyde doc body about AMF."
    assert [c["kind"] for c in llm.calls] == ["chat_stream", "chat"]


async def test_hyde_returns_none_when_both_stream_and_fallback_fail() -> None:
    class _AllBoomLLM(StubLLM):
        async def chat_stream(  # type: ignore[override]
            self, messages: Sequence[dict[str, Any]], **kwargs: Any
        ) -> Any:
            self.calls.append({"kind": "chat_stream", "messages": list(messages), **kwargs})
            raise LLMError("stream down")
            yield

        async def chat(  # type: ignore[override]
            self, messages: Sequence[dict[str, Any]], **kwargs: Any
        ) -> dict[str, Any]:
            self.calls.append({"kind": "chat", "messages": list(messages), **kwargs})
            raise LLMError("nonstream down")

    deps = make_deps(llm=_AllBoomLLM(responses=[]))
    out = await hyde_node(AgentState(user_input="What is SMF?"), deps=deps)
    assert out == {"hyde_doc": None}


async def test_hyde_passes_enough_max_tokens_for_reasoning_model() -> None:
    """回归：AGENT 模型 (mimo-v2.5-pro) 是 reasoning model；早期 max_tokens=600 在
    简单题上就撞顶（reasoning ~250 + 350 content 上限即截断），HyDE doc 不完整影响
    embedding 质量。锁住下限避免回退。流式路径 max_tokens 也要 ≥ 8192。"""
    llm = StubLLM(responses=["fake hyde doc"])
    deps = make_deps(llm=llm)
    await hyde_node(AgentState(user_input="What is AMF?"), deps=deps)
    stream = next(c for c in llm.calls if c["kind"] == "chat_stream")
    assert stream["max_tokens"] >= 8192


async def test_hyde_emits_node_progress_events_when_in_graph() -> None:
    """hyde_node 在 LangGraph 上下文里跑时，每个 chunk 都通过 adispatch_custom_event
    推 node_progress；单测路径调用了 adispatch_custom_event 但不在 callback 上下文，
    会被 contextlib.suppress(RuntimeError) 吞掉 — 这里通过 monkey-patch 验证调用。
    """
    captured: list[dict[str, Any]] = []

    async def _fake_adispatch(name: str, payload: dict[str, Any]) -> None:
        captured.append({"name": name, "payload": payload})

    import app.agent.nodes.hyde as hyde_mod

    orig = hyde_mod.adispatch_custom_event
    hyde_mod.adispatch_custom_event = _fake_adispatch  # type: ignore[assignment]
    try:
        llm = StubLLM(responses=["AMF is the Access and Mobility Management Function."])
        deps = make_deps(llm=llm)
        out = await hyde_node(AgentState(user_input="What is AMF?"), deps=deps)
    finally:
        hyde_mod.adispatch_custom_event = orig  # type: ignore[assignment]

    # StubLLM.chat_stream 把内容拆 ~3 段；至少应该收到 1 条 node_progress
    assert out["hyde_doc"]
    assert len(captured) >= 1
    assert all(c["name"] == "node_progress" for c in captured)
    assert all(c["payload"].get("node") == "hyde" for c in captured)
    # 拼起来等于完整内容
    joined = "".join(c["payload"]["delta"] for c in captured)
    assert joined.strip() == out["hyde_doc"]
