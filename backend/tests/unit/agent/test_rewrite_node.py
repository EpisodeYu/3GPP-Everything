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


async def test_rewrite_disables_thinking_for_determinism() -> None:
    """回归：mimo 思考模式下 temperature=0 被强制 1.0，同题两次改写不一样，
    complex 链路无法复现；reasoning 还会吃光 max_tokens 让 content 空。
    锁住 thinking=disabled + 合理 max_tokens 下限不被无脑往回调。"""
    llm = StubLLM(responses=["rewritten"])
    deps = make_deps(llm=llm)
    await rewrite_node(AgentState(user_input="q"), deps=deps)
    chat = next(c for c in llm.calls if c["kind"] == "chat")
    assert chat["thinking"] == {"type": "disabled"}
    assert chat["max_tokens"] >= 512
