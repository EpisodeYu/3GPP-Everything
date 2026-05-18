"""M4.9 集成测：`/api/v1/messages/{mid}/feedback`。"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.models import Message

from .test_auth import _bootstrap_admin, _login


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _two_user_tokens_and_message(client: Any, db_session: Any) -> tuple[str, str, uuid.UUID]:
    await _bootstrap_admin(client)
    admin = await _login(client, "admin1", "passw0rd!")
    for u in ("alice", "bob"):
        r = await client.post(
            "/api/v1/users",
            json={"username": u, "password": "passw0rd!", "role": "user"},
            headers=_auth_headers(admin["access_token"]),
        )
        assert r.status_code == 201, r.text
    a = (await _login(client, "alice", "passw0rd!"))["access_token"]
    b = (await _login(client, "bob", "passw0rd!"))["access_token"]

    # alice 的 session + message
    r = await client.post("/api/v1/sessions", json={"title": "x"}, headers=_auth_headers(a))
    sid_str = r.json()["id"]
    msg = Message(
        session_id=uuid.UUID(sid_str),
        role="assistant",
        content="some answer",
        status="ok",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return str(a), str(b), msg.id


async def test_create_and_upsert_feedback(client: Any, db_session: Any) -> None:
    a, _, mid = await _two_user_tokens_and_message(client, db_session)
    h = _auth_headers(a)

    r = await client.post(
        f"/api/v1/messages/{mid}/feedback",
        json={"thumb": 1, "reason": "good"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    fid_first = r.json()["id"]
    assert r.json()["thumb"] == 1

    # 第二次提交：upsert（同 id 覆盖）
    r = await client.post(
        f"/api/v1/messages/{mid}/feedback",
        json={"thumb": -1, "reason": "actually bad"},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["id"] == fid_first
    assert r.json()["thumb"] == -1
    assert r.json()["reason"] == "actually bad"


async def test_cannot_feedback_others_message(client: Any, db_session: Any) -> None:
    _, b, mid = await _two_user_tokens_and_message(client, db_session)
    r = await client.post(
        f"/api/v1/messages/{mid}/feedback",
        json={"thumb": 1},
        headers=_auth_headers(b),
    )
    assert r.status_code == 404
    assert r.json()["code"] == "message_not_found"


async def test_feedback_404_for_unknown_message(client: Any, db_session: Any) -> None:
    a, _, _ = await _two_user_tokens_and_message(client, db_session)
    fake = uuid.uuid4()
    r = await client.post(
        f"/api/v1/messages/{fake}/feedback",
        json={"thumb": 1},
        headers=_auth_headers(a),
    )
    assert r.status_code == 404


async def test_feedback_requires_auth(client: Any) -> None:
    r = await client.post(f"/api/v1/messages/{uuid.uuid4()}/feedback", json={"thumb": 1})
    assert r.status_code == 401
