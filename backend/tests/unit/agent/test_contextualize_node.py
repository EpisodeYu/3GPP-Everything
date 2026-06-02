"""contextualize_node：多轮指代消解（2026-06-02 接通真多轮，A 层）。

口径见 docs/03-development/03-agent.md §6.1。验证：
- 无历史 → 不调 LLM，直接回退当前问题（首轮零成本）
- 有历史 → 调 LIGHT 模型 + thinking=disabled，取第一行作自包含问题
- LLM 失败 / 输出空 → 回退原始 user_input（不阻塞主链路）
- 空输入 → contextualized_input 空
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.agent.nodes import contextualize_node
from app.agent.state import AgentState
from app.core.errors import LLMError

from .conftest import StubLLM, make_deps

_HISTORY = [
    {"role": "user", "content": "What is PUCCH-Config?"},
    {"role": "assistant", "content": "PUCCH-Config is an IE defined in 38.331 [1]."},
]


async def test_no_history_skips_llm_and_returns_user_input() -> None:
    llm = StubLLM(responses=["should NOT be called"])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="它的默认值是多少?")  # 无 history

    out = await contextualize_node(state, deps=deps)
    assert out["contextualized_input"] == "它的默认值是多少?"
    # 首轮零额外 LLM 调用
    assert not [c for c in llm.calls if c["kind"] in ("chat", "chat_stream")]


async def test_with_history_resolves_reference() -> None:
    resolved = "What is the default value of PUCCH-Config?"
    llm = StubLLM(responses=[resolved])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="它的默认值是多少?", history=_HISTORY)

    out = await contextualize_node(state, deps=deps)
    assert out["contextualized_input"] == resolved
    chat = next(c for c in llm.calls if c["kind"] == "chat")
    # 用 LIGHT 模型 + thinking=disabled（可复现）
    assert chat["model"] == deps.settings.LLM_LIGHT_MODEL
    assert chat["thinking"] == {"type": "disabled"}
    # prompt 里应带上历史与当前问题
    prompt = chat["messages"][0]["content"]
    assert "PUCCH-Config" in prompt
    assert "它的默认值是多少?" in prompt


async def test_takes_first_nonempty_line_and_strips_quotes() -> None:
    llm = StubLLM(responses=['"Resolved standalone question"\n(ignored second line)'])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="follow up", history=_HISTORY)

    out = await contextualize_node(state, deps=deps)
    assert out["contextualized_input"] == "Resolved standalone question"


async def test_llm_failure_falls_back_to_user_input() -> None:
    class _BoomLLM(StubLLM):
        async def chat(self, messages: Sequence[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.calls.append({"kind": "chat", "messages": list(messages), **kwargs})
            raise LLMError("network down")

    llm = _BoomLLM(responses=[""])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="它的默认值?", history=_HISTORY)

    out = await contextualize_node(state, deps=deps)
    assert out["contextualized_input"] == "它的默认值?"


async def test_empty_output_falls_back_to_user_input() -> None:
    llm = StubLLM(responses=["   \n  "])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="它的默认值?", history=_HISTORY)

    out = await contextualize_node(state, deps=deps)
    assert out["contextualized_input"] == "它的默认值?"


async def test_blank_user_input_returns_empty() -> None:
    llm = StubLLM(responses=["x"])
    deps = make_deps(llm=llm)
    out = await contextualize_node(AgentState(user_input="   ", history=_HISTORY), deps=deps)
    assert out["contextualized_input"] == ""
