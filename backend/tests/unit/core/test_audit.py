"""core/audit.py 单测：write_audit 把行加到 session，flush 不 commit。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.audit import write_audit
from app.db.models import AuditLog


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flush_calls = 0
        self.commit_calls = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1


async def test_write_audit_inserts_row_and_flushes() -> None:
    db = FakeSession()
    actor = uuid.uuid4()
    row = await write_audit(
        db,  # type: ignore[arg-type]
        actor_user_id=actor,
        action="user.create",
        target_type="user",
        target_id=str(uuid.uuid4()),
        ip="127.0.0.1",
        user_agent="pytest",
        extra={"role": "user"},
    )
    assert isinstance(row, AuditLog)
    assert len(db.added) == 1
    assert db.added[0] is row
    assert db.flush_calls == 1
    assert db.commit_calls == 0  # write_audit 不应 commit
    assert row.action == "user.create"
    assert row.extra == {"role": "user"}


async def test_write_audit_defaults_empty_extra() -> None:
    db = FakeSession()
    row = await write_audit(db, actor_user_id=None, action="bootstrap_admin.failed")  # type: ignore[arg-type]
    assert row.extra == {}
    assert row.actor_user_id is None


@pytest.mark.parametrize(
    "action",
    [
        "user.create",
        "user.disable",
        "user.role_change",
        "user.logout",
        "bootstrap_admin.success",
    ],
)
async def test_action_values_accepted(action: str) -> None:
    db = FakeSession()
    row = await write_audit(db, actor_user_id=None, action=action)  # type: ignore[arg-type]
    assert row.action == action
