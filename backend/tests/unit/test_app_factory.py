"""create_app 工厂：Swagger / OpenAPI 仅 dev 暴露，prod 关闭（信息暴露面）。

create_app 在函数体内直接调 `get_settings()`（非请求级 DI），所以这里 monkeypatch
`app.main.get_settings` 注入不同 APP_ENV 的 Settings 再构造 app。
"""

from __future__ import annotations

from typing import Any

import app.main as main_mod
from app.core.config import Settings


def _settings(env: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        APP_ENV=env,  # type: ignore[arg-type]
        APP_SECRET_KEY="test-secret-32-bytes-padding-padding",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LANGFUSE_PUBLIC_KEY="",
        LANGFUSE_SECRET_KEY="",
    )


def test_docs_enabled_in_dev(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_mod, "get_settings", lambda: _settings("dev"))
    app = main_mod.create_app()
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


def test_docs_disabled_in_prod(monkeypatch: Any) -> None:
    monkeypatch.setattr(main_mod, "get_settings", lambda: _settings("prod"))
    app = main_mod.create_app()
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


async def test_lifespan_shuts_down_langfuse_exporter(monkeypatch: Any) -> None:
    import app.agent.langfuse_handler as langfuse_handler

    calls: list[None] = []
    monkeypatch.setattr(langfuse_handler, "shutdown_langfuse", lambda: calls.append(None))
    monkeypatch.setattr(main_mod, "get_settings", lambda: _settings("dev"))
    app = main_mod.create_app()
    app.state.disable_agent_init = True

    async with main_mod.lifespan(app):
        pass

    assert calls == [None]
