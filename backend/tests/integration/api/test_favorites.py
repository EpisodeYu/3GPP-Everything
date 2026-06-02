"""M4.9 集成测：`/api/v1/favorites` CRUD + 跨用户隔离。"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.models import Message

from .test_auth import _bootstrap_admin, _login


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _two_user_tokens(client: Any) -> tuple[str, str]:
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
    return str(a), str(b)


async def test_create_list_delete_favorite(client: Any) -> None:
    a, _ = await _two_user_tokens(client)
    h = _auth_headers(a)

    r = await client.post(
        "/api/v1/favorites",
        json={"target_type": "chunk", "target_id": "c-001"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    fid = r.json()["id"]

    r = await client.get("/api/v1/favorites", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["target_id"] == "c-001"

    r = await client.get("/api/v1/favorites?target_type=message", headers=h)
    assert r.json()["items"] == []

    r = await client.delete(f"/api/v1/favorites/{fid}", headers=h)
    assert r.status_code == 204

    r = await client.delete(f"/api/v1/favorites/{fid}", headers=h)
    assert r.status_code == 404


async def test_users_cannot_see_others_favorites(client: Any) -> None:
    a, b = await _two_user_tokens(client)
    await client.post(
        "/api/v1/favorites",
        json={"target_type": "chunk", "target_id": "c-x"},
        headers=_auth_headers(a),
    )
    r = await client.get("/api/v1/favorites", headers=_auth_headers(b))
    assert r.status_code == 200
    assert r.json()["items"] == []


async def test_favorites_require_auth(client: Any) -> None:
    r = await client.get("/api/v1/favorites")
    assert r.status_code == 401


async def test_message_favorite_enriched_chunk_not(client: Any, db_session: Any) -> None:
    """message 收藏 list 时带回 session_id + 预览；chunk 收藏两者为 null。"""
    a, _ = await _two_user_tokens(client)
    h = _auth_headers(a)

    r = await client.post("/api/v1/sessions", json={"title": "x"}, headers=h)
    sid = r.json()["id"]
    msg = Message(
        session_id=uuid.UUID(sid),
        role="assistant",
        content="3GPP NR PDCP 详解 " * 30,
        status="ok",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    await client.post(
        "/api/v1/favorites",
        json={"target_type": "message", "target_id": str(msg.id)},
        headers=h,
    )
    await client.post(
        "/api/v1/favorites",
        json={"target_type": "chunk", "target_id": "c-1"},
        headers=h,
    )

    r = await client.get("/api/v1/favorites", headers=h)
    items = {it["target_id"]: it for it in r.json()["items"]}
    msg_item = items[str(msg.id)]
    assert msg_item["session_id"] == sid
    assert msg_item["preview"].startswith("3GPP NR PDCP")
    assert len(msg_item["preview"]) <= 140
    chunk_item = items["c-1"]
    assert chunk_item["session_id"] is None
    assert chunk_item["preview"] is None
