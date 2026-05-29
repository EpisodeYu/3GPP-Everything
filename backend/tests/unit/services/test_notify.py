"""services/notify.py 单测：Server酱 推送（含 SendKey 的 URL；空则 no-op）。

用 httpx.MockTransport 注入假传输，不发真实网络请求。
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.services import notify


async def test_skip_when_url_empty() -> None:
    # 显式空 URL → 直接 no-op，返回 False，不发请求
    assert await notify.send_serverchan("t", "d", url="") is False


async def test_posts_title_and_desp() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"code": 0, "message": "OK"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = await notify.send_serverchan(
        "标题x", "正文y", url="https://sctapi.ftqq.com/SCTKEY.send", client=client
    )
    await client.aclose()

    assert ok is True
    assert "SCTKEY.send" in captured["url"]  # type: ignore[operator]
    body = captured["body"]
    assert "title=" in body and "desp=" in body  # type: ignore[operator]


async def test_returns_false_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = await notify.send_serverchan("t", "d", url="https://x/y.send", client=client)
    await client.aclose()
    assert ok is False  # 5xx 被 raise_for_status 捕获 → swallow


def _settings(url: str) -> Settings:
    return Settings(_env_file=None, SERVERCHAN_URL=url)  # type: ignore[call-arg]


def test_schedule_returns_none_when_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "get_settings", lambda: _settings(""))
    assert notify.schedule_serverchan("t", "d") is None


async def test_schedule_creates_task_when_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "get_settings", lambda: _settings("https://x/y.send"))

    calls: list[tuple[str, str]] = []

    async def fake_send(title: str, desp: str = "", *, url: str | None = None) -> bool:
        calls.append((title, desp))
        return True

    monkeypatch.setattr(notify, "send_serverchan", fake_send)
    task = notify.schedule_serverchan("hi", "there")
    assert task is not None
    result = await task
    assert result is True
    assert calls == [("hi", "there")]
