"""Tool 单测共用：FakeSessionmaker (返回预设 rows) + 复用 agent conftest。"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


@dataclass
class _FakeScalar:
    rows: list[Any]

    def all(self) -> list[Any]:
        return list(self.rows)


@dataclass
class _FakeResult:
    rows: list[Any]

    def scalars(self) -> _FakeScalar:
        return _FakeScalar(self.rows)


class FakeSession:
    """模拟 AsyncSession.execute(stmt) → result.scalars().all() 的最小子集。"""

    def __init__(self, rows: Iterable[Any]) -> None:
        self._rows = list(rows)
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        return _FakeResult(self._rows)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeSessionmaker:
    """async_sessionmaker() 调用返回 async-context-manager session。"""

    def __init__(self, rows: Iterable[Any] | None = None, *, raises: Exception | None = None):
        self._rows = list(rows or [])
        self._raises = raises
        self.created: list[FakeSession] = []

    def __call__(self) -> Any:
        if self._raises is not None:
            err = self._raises

            @asynccontextmanager
            async def _bad():  # type: ignore[no-untyped-def]
                raise err
                yield  # pragma: no cover

            return _bad()
        sess = FakeSession(self._rows)
        self.created.append(sess)
        return sess
