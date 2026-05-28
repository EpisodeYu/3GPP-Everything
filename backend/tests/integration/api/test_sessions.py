"""M4.7 集成测：`/api/v1/sessions` CRUD + archived_branch 守卫。

文档锚 04-backend-api.md §M4.7。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update

from app.db.models import Session as DBSession

from .test_auth import _bootstrap_admin, _login


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _new_user_token(client: Any, username: str = "u1") -> str:
    await _bootstrap_admin(client)
    admin = await _login(client, "admin1", "passw0rd!")
    res = await client.post(
        "/api/v1/users",
        json={"username": username, "password": "passw0rd!", "role": "user"},
        headers=_auth_headers(admin["access_token"]),
    )
    assert res.status_code == 201, res.text
    out = await _login(client, username, "passw0rd!")
    return str(out["access_token"])


async def test_create_list_get_patch_delete(client: Any) -> None:
    token = await _new_user_token(client)
    h = _auth_headers(token)

    # create
    r = await client.post(
        "/api/v1/sessions", json={"title": "hello", "mode_default": "qa"}, headers=h
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["title"] == "hello"
    assert r.json()["status"] == "active"

    # list
    r = await client.get("/api/v1/sessions", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == sid

    # get
    r = await client.get(f"/api/v1/sessions/{sid}", headers=h)
    assert r.status_code == 200
    assert r.json()["id"] == sid

    # patch title
    r = await client.patch(f"/api/v1/sessions/{sid}", json={"title": "renamed"}, headers=h)
    assert r.status_code == 200
    assert r.json()["title"] == "renamed"

    # delete
    r = await client.delete(f"/api/v1/sessions/{sid}", headers=h)
    assert r.status_code == 204

    r = await client.get(f"/api/v1/sessions/{sid}", headers=h)
    assert r.status_code == 404


async def test_archived_branch_rejects_title_patch_but_allows_delete(
    client: Any, db_session: Any
) -> None:
    token = await _new_user_token(client)
    h = _auth_headers(token)
    r = await client.post("/api/v1/sessions", json={"title": "x"}, headers=h)
    sid = r.json()["id"]

    # 直接改状态模拟 M4.8 fork 后归档
    await db_session.execute(
        update(DBSession).where(DBSession.id == uuid.UUID(sid)).values(status="archived_branch")
    )
    await db_session.commit()

    # title patch 应 409
    r = await client.patch(f"/api/v1/sessions/{sid}", json={"title": "nope"}, headers=h)
    assert r.status_code == 409
    assert r.json()["code"] == "session_archived"

    # 但 delete 允许
    r = await client.delete(f"/api/v1/sessions/{sid}", headers=h)
    assert r.status_code == 204


async def test_user_cannot_see_others_sessions(client: Any) -> None:
    await _bootstrap_admin(client)
    admin = await _login(client, "admin1", "passw0rd!")
    for u in ("alice", "bob"):
        r = await client.post(
            "/api/v1/users",
            json={"username": u, "password": "passw0rd!", "role": "user"},
            headers=_auth_headers(admin["access_token"]),
        )
        assert r.status_code == 201, r.text

    a_token = (await _login(client, "alice", "passw0rd!"))["access_token"]
    b_token = (await _login(client, "bob", "passw0rd!"))["access_token"]

    r = await client.post(
        "/api/v1/sessions", json={"title": "alices"}, headers=_auth_headers(a_token)
    )
    a_sid = r.json()["id"]

    r = await client.get("/api/v1/sessions", headers=_auth_headers(b_token))
    assert r.json()["total"] == 0

    r = await client.get(f"/api/v1/sessions/{a_sid}", headers=_auth_headers(b_token))
    assert r.status_code == 404


async def test_session_routes_require_auth(client: Any) -> None:
    r = await client.get("/api/v1/sessions")
    assert r.status_code == 401
    r = await client.post("/api/v1/sessions", json={"title": "x"})
    assert r.status_code == 401
    r = await client.delete("/api/v1/sessions")
    assert r.status_code == 401


async def test_delete_all_sessions_clears_only_caller(client: Any) -> None:
    """DELETE /sessions 清掉调用者自己的所有会话，不影响其他用户。"""
    await _bootstrap_admin(client)
    admin = await _login(client, "admin1", "passw0rd!")
    for u in ("alice", "bob"):
        r = await client.post(
            "/api/v1/users",
            json={"username": u, "password": "passw0rd!", "role": "user"},
            headers=_auth_headers(admin["access_token"]),
        )
        assert r.status_code == 201

    a_token = (await _login(client, "alice", "passw0rd!"))["access_token"]
    b_token = (await _login(client, "bob", "passw0rd!"))["access_token"]

    for title in ("a1", "a2", "a3"):
        r = await client.post(
            "/api/v1/sessions", json={"title": title}, headers=_auth_headers(a_token)
        )
        assert r.status_code == 201
    r = await client.post("/api/v1/sessions", json={"title": "b1"}, headers=_auth_headers(b_token))
    assert r.status_code == 201

    r = await client.delete("/api/v1/sessions", headers=_auth_headers(a_token))
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 3}

    r = await client.get("/api/v1/sessions", headers=_auth_headers(a_token))
    assert r.json()["total"] == 0
    # bob 的会话保留
    r = await client.get("/api/v1/sessions", headers=_auth_headers(b_token))
    assert r.json()["total"] == 1


async def test_delete_all_sessions_when_empty_returns_zero(client: Any) -> None:
    """无会话时返回 deleted=0，不报错。"""
    token = await _new_user_token(client)
    r = await client.delete("/api/v1/sessions", headers=_auth_headers(token))
    assert r.status_code == 200
    assert r.json() == {"deleted": 0}


async def test_delete_all_sessions_removes_sessions_with_messages(
    client: Any, db_session: Any
) -> None:
    """带 messages 的会话清空：session 行确实从 DB 删掉。

    messages 行的 cascade 依赖 schema 上 `ON DELETE CASCADE` —— 在生产 PG/MySQL
    工作；本测试跑在 SQLite in-memory 上 FK 默认关闭，仅断言 session 本身被删，
    不强求 messages cascade（cascade 测试见 PG 集成回归 / production 行为）。
    """
    from app.db.models import Message

    token = await _new_user_token(client)
    h = _auth_headers(token)
    r = await client.post("/api/v1/sessions", json={"title": "x"}, headers=h)
    sid = r.json()["id"]
    db_session.add(Message(session_id=uuid.UUID(sid), role="user", content="hi", status="ok"))
    await db_session.commit()

    r = await client.delete("/api/v1/sessions", headers=h)
    assert r.status_code == 200
    assert r.json()["deleted"] == 1

    # session 行确实被删
    res = await db_session.execute(select(DBSession).where(DBSession.id == uuid.UUID(sid)))
    assert res.scalar_one_or_none() is None
