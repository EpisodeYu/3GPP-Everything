"""SQLAlchemy 2.0 async base / engine / session 工厂。

设计要点：
- `Base` = `DeclarativeBase`，所有 ORM 模型挂这棵树
- engine 单例（lru_cache）；DATABASE_URL 从 settings 读
- `get_db()` 是 FastAPI `Depends` 用的 async 生成器
- `metadata` 命名约定固定（让 alembic autogenerate 出稳定 constraint name）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

# alembic autogenerate 的约束 / 索引命名（避免 sqlite ↔ pg 之间漂移）
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    s = get_settings()
    return create_async_engine(
        s.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI Depends 入口。"""
    sm = get_sessionmaker()
    async with sm() as session:
        yield session
