"""web_search_tool 单测：tavily client monkeypatch；prefix 必出现。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from app.agent.state import AgentState
from app.tools import web_search as web_search_mod

from ..agent.conftest import make_deps


class _StubTavily:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search(self, *, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kwargs})
        return {
            "results": [
                {
                    "title": "3GPP TS 38.331 v18 release notes",
                    "url": "https://example.com/3gpp",
                    "content": "Latest release of NR RRC ...",
                }
            ]
        }


async def test_web_search_tool_no_api_key_returns_warning() -> None:
    deps = make_deps()
    # 默认 settings.TAVILY_API_KEY 是空 SecretStr
    out = await web_search_mod.web_search_tool(AgentState(user_input="latest 5G news"), deps=deps)
    assert out["results"] == []
    assert out["warning"] == "TAVILY_API_KEY not set"
    assert out["prefix"].startswith("以下内容来自 Web 搜索")


async def test_web_search_tool_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    deps = make_deps()
    deps.settings = deps.settings.model_copy(update={"TAVILY_API_KEY": SecretStr("test-key")})
    stub = _StubTavily()
    monkeypatch.setattr(web_search_mod, "_build_client", lambda key: stub)
    out = await web_search_mod.web_search_tool(AgentState(user_input="5G slicing news"), deps=deps)
    assert out["warning"] is None
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["url"] == "https://example.com/3gpp"
    assert "3GPP" in r["title"]
    assert stub.calls and stub.calls[0]["query"] == "5G slicing news"


async def test_web_search_tool_client_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    deps = make_deps()
    deps.settings = deps.settings.model_copy(update={"TAVILY_API_KEY": SecretStr("key")})
    monkeypatch.setattr(web_search_mod, "_build_client", lambda key: None)
    out = await web_search_mod.web_search_tool(AgentState(user_input="x"), deps=deps)
    assert out["results"] == []
    assert out["warning"] == "tavily-python unavailable"


async def test_web_search_tool_search_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomTavily:
        def search(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("boom")

    deps = make_deps()
    deps.settings = deps.settings.model_copy(update={"TAVILY_API_KEY": SecretStr("key")})
    monkeypatch.setattr(web_search_mod, "_build_client", lambda key: _BoomTavily())
    out = await web_search_mod.web_search_tool(AgentState(user_input="x"), deps=deps)
    assert out["results"] == []
    assert "boom" in (out["warning"] or "")
