"""hyde_node 单测（M4.3 complex 分支）。"""

from __future__ import annotations

from app.agent.nodes import hyde_node
from app.agent.state import AgentState
from app.core.errors import LLMError

from .conftest import StubLLM, make_deps


async def test_hyde_sets_state_hyde_doc() -> None:
    fake_doc = (
        "The Access and Mobility Management Function (AMF) terminates the N1/N2 interfaces "
        "and handles registration, connection, mobility and access authentication for UEs ..."
    )
    llm = StubLLM(responses=[fake_doc])
    deps = make_deps(llm=llm)
    state = AgentState(user_input="What is AMF?")
    out = await hyde_node(state, deps=deps)
    assert out["hyde_doc"] == fake_doc.strip()
    # 使用 agent / pro 模型（hyde 需要生成质量更高的伪文档）
    chat = next(c for c in llm.calls if c["kind"] == "chat")
    assert chat["model"] == deps.settings.LLM_AGENT_MODEL


async def test_hyde_empty_user_input_returns_none() -> None:
    llm = StubLLM(responses=["should never be used"])
    deps = make_deps(llm=llm)
    out = await hyde_node(AgentState(user_input=""), deps=deps)
    assert out == {"hyde_doc": None}
    assert llm.calls == []  # 空输入不应调 LLM


async def test_hyde_llm_failure_returns_none() -> None:
    class FailingLLM(StubLLM):
        async def chat(self, *args, **kwargs):  # type: ignore[override]
            raise LLMError("boom")

    deps = make_deps(llm=FailingLLM(responses=[]))
    out = await hyde_node(AgentState(user_input="What is SMF?"), deps=deps)
    assert out == {"hyde_doc": None}
