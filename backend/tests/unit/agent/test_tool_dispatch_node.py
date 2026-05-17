"""tool_dispatch_node 单测：显式触发守约 + 并发跑 + 异常隔离。"""

from __future__ import annotations

import pytest

from app.agent.nodes.tool_dispatch import tool_dispatch_node
from app.agent.state import AgentState
from app.tools import TOOL_REGISTRY

from .conftest import make_deps


async def test_tool_dispatch_skips_when_no_explicit_tools() -> None:
    deps = make_deps()
    state = AgentState(user_input="What is AMF?")
    out = await tool_dispatch_node(state, deps=deps)
    assert out == {"tool_results": {}}


async def test_tool_dispatch_runs_only_registered_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_glossary(state, deps):  # type: ignore[no-untyped-def]
        calls.append("glossary")
        return {"matches": [{"term": "AMF"}], "warning": None}

    async def fake_toc(state, deps):  # type: ignore[no-untyped-def]
        calls.append("toc")
        return {"items": [], "warning": None}

    monkeypatch.setitem(TOOL_REGISTRY, "glossary", fake_glossary)
    monkeypatch.setitem(TOOL_REGISTRY, "toc", fake_toc)

    deps = make_deps()
    state = AgentState(
        user_input="What is AMF?",
        explicit_tools=["glossary", "toc", "nonexistent_tool"],
    )
    out = await tool_dispatch_node(state, deps=deps)
    # 未注册的 tool 直接被忽略；glossary/toc 都跑了
    assert set(calls) == {"glossary", "toc"}
    assert set(out["tool_results"].keys()) == {"glossary", "toc"}
    assert out["tool_results"]["glossary"]["matches"][0]["term"] == "AMF"


async def test_tool_dispatch_isolates_tool_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(state, deps):  # type: ignore[no-untyped-def]
        raise ValueError("kaboom")

    async def ok(state, deps):  # type: ignore[no-untyped-def]
        return {"x": 1}

    monkeypatch.setitem(TOOL_REGISTRY, "glossary", boom)
    monkeypatch.setitem(TOOL_REGISTRY, "toc", ok)

    deps = make_deps()
    state = AgentState(user_input="q", explicit_tools=["glossary", "toc"])
    out = await tool_dispatch_node(state, deps=deps)
    # glossary 抛了，但 toc 仍跑成功；glossary 结果带 warning
    assert "toc" in out["tool_results"]
    assert out["tool_results"]["toc"] == {"x": 1}
    assert "warning" in out["tool_results"]["glossary"]
    assert "kaboom" in out["tool_results"]["glossary"]["warning"]


async def test_tool_dispatch_dedups_explicit_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_glossary(state, deps):  # type: ignore[no-untyped-def]
        calls.append("glossary")
        return {"matches": []}

    monkeypatch.setitem(TOOL_REGISTRY, "glossary", fake_glossary)

    deps = make_deps()
    state = AgentState(user_input="x", explicit_tools=["glossary", "glossary"])
    await tool_dispatch_node(state, deps=deps)
    assert calls == ["glossary"]
