"""rewrite_node：M4.3 complex 分支用，M4.2 仍通过单测确保实现可用。"""

from __future__ import annotations

from app.agent.nodes import rewrite_node
from app.agent.state import AgentState

from .conftest import StubLLM, make_deps


async def test_rewrite_uses_first_line() -> None:
    llm = StubLLM(responses=["5G registration procedure step by step"])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="5G 注册流程是什么样的")

    out = await rewrite_node(state, deps=deps)
    assert out["rewritten_queries"] == ["5G registration procedure step by step"]


async def test_rewrite_falls_back_to_user_input_on_empty() -> None:
    llm = StubLLM(responses=[""])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="some 3gpp question")
    out = await rewrite_node(state, deps=deps)
    assert out["rewritten_queries"] == ["some 3gpp question"]


async def test_rewrite_passes_enough_max_tokens_for_reasoning_model() -> None:
    """回归：LIGHT 模型是 reasoning model，max_tokens 太低会被 reasoning 吃光导致
    content 永远空 → rewrite 永远 no-op。锁住下限避免再次回退到 120。"""
    llm = StubLLM(responses=["rewritten"])
    deps = make_deps(llm=llm)
    await rewrite_node(AgentState(user_input="q"), deps=deps)
    chat = next(c for c in llm.calls if c["kind"] == "chat")
    assert chat["max_tokens"] >= 4096
