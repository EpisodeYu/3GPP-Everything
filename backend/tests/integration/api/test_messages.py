"""F-5 / F-6 集成测：`/api/v1/sessions/{sid}/messages` 列表 + 详情。

锚 04-backend-api.md §2 路由总表 + 2026-05-19 端到端人审 finding F-5/F-6。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.models import Message, MessageCitation
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


async def _resolve_user_id(db_session: Any, username: str) -> uuid.UUID:
    from sqlalchemy import select

    from app.db.models import User

    res = await db_session.execute(select(User).where(User.username == username))
    user = res.scalar_one()
    return user.id


async def _seed_session_with_messages(
    db_session: Any, user_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """返回 (session_id, assistant_message_id)。"""
    s = DBSession(user_id=user_id, title="t1", mode_default="qa")
    db_session.add(s)
    await db_session.flush()
    sid = s.id

    user_msg = Message(
        session_id=sid,
        role="user",
        content="What is AMF?",
        status="ok",
    )
    assistant_msg = Message(
        session_id=sid,
        role="assistant",
        content="AMF stands for Access and Mobility Management Function.",
        status="ok",
        confidence=0.88,
        self_rag_verdict="accept",
        langgraph_run_id="run-abc",
    )
    db_session.add_all([user_msg, assistant_msg])
    await db_session.flush()

    db_session.add(
        MessageCitation(
            message_id=assistant_msg.id,
            chunk_id="c-amf-1",
            rank=0,
            rerank_score=0.91,
            spec_id="23.501",
            section_path="6.3.1",
        )
    )
    await db_session.commit()
    return sid, assistant_msg.id


async def test_list_messages_returns_ordered_pair(client: Any, db_session: Any) -> None:
    token = await _new_user_token(client)
    user_id = await _resolve_user_id(db_session, "u1")
    sid, _ = await _seed_session_with_messages(db_session, user_id)

    r = await client.get(f"/api/v1/sessions/{sid}/messages", headers=_auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    roles = [m["role"] for m in body["items"]]
    assert roles == ["user", "assistant"]
    assistant = body["items"][1]
    assert assistant["content"].startswith("AMF stands for")
    assert assistant["langgraph_run_id"] == "run-abc"
    # citations 跟随 assistant message
    assert len(assistant["citations"]) == 1
    assert assistant["citations"][0]["chunk_id"] == "c-amf-1"
    assert assistant["citations"][0]["spec_id"] == "23.501"


async def test_get_message_returns_with_citations(client: Any, db_session: Any) -> None:
    token = await _new_user_token(client)
    user_id = await _resolve_user_id(db_session, "u1")
    sid, mid = await _seed_session_with_messages(db_session, user_id)

    r = await client.get(f"/api/v1/sessions/{sid}/messages/{mid}", headers=_auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(mid)
    assert body["role"] == "assistant"
    assert body["citations"][0]["chunk_id"] == "c-amf-1"
    assert body["citations"][0]["rerank_score"] == 0.91


async def test_get_message_404_for_unknown(client: Any, db_session: Any) -> None:
    token = await _new_user_token(client)
    user_id = await _resolve_user_id(db_session, "u1")
    sid, _ = await _seed_session_with_messages(db_session, user_id)

    bogus = uuid.uuid4()
    r = await client.get(f"/api/v1/sessions/{sid}/messages/{bogus}", headers=_auth_headers(token))
    assert r.status_code == 404
    assert r.json()["code"] == "message_not_found"


async def test_other_user_cannot_see_messages(client: Any, db_session: Any) -> None:
    token1 = await _new_user_token(client, username="u1")
    user1_id = await _resolve_user_id(db_session, "u1")
    sid, mid = await _seed_session_with_messages(db_session, user1_id)

    # u2 用同套 client → /users POST 需要 admin token；走 helper 创建第二个普通用户
    admin = await _login(client, "admin1", "passw0rd!")
    await client.post(
        "/api/v1/users",
        json={"username": "u2", "password": "passw0rd!", "role": "user"},
        headers=_auth_headers(admin["access_token"]),
    )
    out2 = await _login(client, "u2", "passw0rd!")
    token2 = str(out2["access_token"])

    # u2 看 u1 的 session → 404 session_not_found（路由权属校验，不暴露存在性）
    r = await client.get(f"/api/v1/sessions/{sid}/messages", headers=_auth_headers(token2))
    assert r.status_code == 404
    r = await client.get(f"/api/v1/sessions/{sid}/messages/{mid}", headers=_auth_headers(token2))
    assert r.status_code == 404

    # u1 自己看正常
    assert token1
    r = await client.get(f"/api/v1/sessions/{sid}/messages", headers=_auth_headers(token1))
    assert r.status_code == 200


async def test_messages_require_auth(client: Any) -> None:
    fake_sid = uuid.uuid4()
    fake_mid = uuid.uuid4()
    r = await client.get(f"/api/v1/sessions/{fake_sid}/messages")
    assert r.status_code == 401
    r = await client.get(f"/api/v1/sessions/{fake_sid}/messages/{fake_mid}")
    assert r.status_code == 401
