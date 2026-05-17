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
