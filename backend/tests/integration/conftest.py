"""集成测公共脚手架。

- `client` / `db_session` fixture：用 aiosqlite (file::memory:?cache=shared) 起一份临时库
  + create_all 全部 ORM 表；override `get_db` / `get_settings` / Redis 客户端
- in-memory Redis stub 注入到 `app.state.redis`

为什么不连真实 PG / Redis：M4.6 集成测目标是验证路由 + 鉴权 + 审计 + 限流的契约，
不验证 SQL 兼容性（那是 alembic 迁移测的事）。SQLite 路径快，CI 不需要起 PG。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.base import get_db
from app.db.models import Base


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """所有 tests/integration/api/ 下的用例自动标 integration。"""
    for item in items:
        if "/tests/integration/api/" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


class FakeRedis:
    """app.core.ratelimit.consume 用的最小 stub。"""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True


def _test_settings() -> Settings:
    return Settings(
        APP_SECRET_KEY="test-secret-32-bytes-padding-padding",
        ACCESS_TOKEN_EXPIRE_MINUTES=15,
        REFRESH_TOKEN_EXPIRE_DAYS=7,
        BOOTSTRAP_ADMIN_INVITE_CODE="invite-code-for-tests",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
    )


@pytest_asyncio.fixture
async def app_and_state() -> AsyncIterator[tuple[Any, FakeRedis, async_sessionmaker[AsyncSession]]]:
    """每个测试一份独立 app / DB / Redis。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    settings = _test_settings()
    fake_redis = FakeRedis()

    # 延迟导入避免 main import 时尚未 override
    from app.main import create_app

    app = create_app()

    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.redis = fake_redis

    try:
        yield app, fake_redis, sessionmaker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(app_and_state: Any) -> AsyncIterator[AsyncClient]:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def db_session(app_and_state: Any) -> AsyncIterator[AsyncSession]:
    _, _, sm = app_and_state
    async with sm() as s:
        yield s


@pytest.fixture(scope="session")
def event_loop() -> Any:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
