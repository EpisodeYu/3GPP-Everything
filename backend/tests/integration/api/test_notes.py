"""M4.9 集成测：`/api/v1/notes` CRUD。"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.models import Message

from .test_auth import _bootstrap_admin, _login


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _new_user_token(client: Any) -> str:
    await _bootstrap_admin(client)
    admin = await _login(client, "admin1", "passw0rd!")
    res = await client.post(
        "/api/v1/users",
        json={"username": "u1", "password": "passw0rd!", "role": "user"},
        headers=_auth_headers(admin["access_token"]),
    )
    assert res.status_code == 201, res.text
    out = await _login(client, "u1", "passw0rd!")
    return str(out["access_token"])


async def test_full_note_lifecycle(client: Any) -> None:
    token = await _new_user_token(client)
    h = _auth_headers(token)

    r = await client.post(
        "/api/v1/notes",
        json={"target_type": "chunk", "target_id": "c-1", "body": "first thought"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    nid = r.json()["id"]
    assert r.json()["body"] == "first thought"

    r = await client.get("/api/v1/notes", headers=h)
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1

    r = await client.get("/api/v1/notes?target_type=chunk&target_id=c-1", headers=h)
    assert len(r.json()["items"]) == 1
    r = await client.get("/api/v1/notes?target_type=chunk&target_id=c-99", headers=h)
    assert r.json()["items"] == []

    r = await client.patch(f"/api/v1/notes/{nid}", json={"body": "updated"}, headers=h)
    assert r.status_code == 200
    assert r.json()["body"] == "updated"

    r = await client.delete(f"/api/v1/notes/{nid}", headers=h)
    assert r.status_code == 204

    r = await client.patch(f"/api/v1/notes/{nid}", json={"body": "x"}, headers=h)
    assert r.status_code == 404


async def test_notes_require_auth(client: Any) -> None:
    r = await client.get("/api/v1/notes")
    assert r.status_code == 401


async def test_message_note_enriched(client: Any, db_session: Any) -> None:
    """message 笔记 list 时带回 session_id + 预览；chunk 笔记两者为 null。"""
    token = await _new_user_token(client)
    h = _auth_headers(token)

    r = await client.post("/api/v1/sessions", json={"title": "x"}, headers=h)
    sid = r.json()["id"]
    msg = Message(
        session_id=uuid.UUID(sid),
        role="assistant",
        content="answer body here",
        status="ok",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    await client.post(
        "/api/v1/notes",
        json={"target_type": "message", "target_id": str(msg.id), "body": "my note"},
        headers=h,
    )
    await client.post(
        "/api/v1/notes",
        json={"target_type": "chunk", "target_id": "c-1", "body": "chunk note"},
        headers=h,
    )

    r = await client.get("/api/v1/notes", headers=h)
    items = {it["target_id"]: it for it in r.json()["items"]}
    msg_item = items[str(msg.id)]
    assert msg_item["session_id"] == sid
    assert msg_item["preview"] == "answer body here"
    assert msg_item["body"] == "my note"
    chunk_item = items["c-1"]
    assert chunk_item["session_id"] is None
    assert chunk_item["preview"] is None
