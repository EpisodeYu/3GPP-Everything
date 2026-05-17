"""M4.6 鉴权集成测：bootstrap / login / refresh / logout / RBAC / 停用 / 限流 / 审计。

文档锚 04-backend-api.md §M4.6 验收清单。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import AuditLog, RefreshToken, User


async def _bootstrap_admin(
    client: Any,
    username: str = "admin1",
    password: str = "passw0rd!",
) -> dict[str, Any]:
    res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "username": username,
            "password": password,
            "invite_code": "invite-code-for-tests",
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


async def _login(client: Any, username: str, password: str) -> dict[str, Any]:
    res = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()


# === bootstrap-admin ===


async def test_bootstrap_admin_creates_first_admin_then_409(client: Any) -> None:
    body = await _bootstrap_admin(client)
    assert body["role"] == "admin"
    # 二次调用 → 409
    res2 = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "username": "admin2",
            "password": "passw0rd!",
            "invite_code": "invite-code-for-tests",
        },
    )
    assert res2.status_code == 409
    assert res2.json()["code"] == "already_initialized"


async def test_bootstrap_admin_bad_invite_code_returns_401(client: Any) -> None:
    res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={"username": "x", "password": "passw0rd!", "invite_code": "wrong"},
    )
    assert res.status_code == 401
    assert res.json()["code"] == "invalid_invite_code"


# === 密码策略 (Q3) ===


async def test_password_too_short_returns_422(client: Any) -> None:
    res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={"username": "x", "password": "short", "invite_code": "invite-code-for-tests"},
    )
    assert res.status_code == 422


# === login / refresh / logout / me ===


async def test_login_returns_token_pair_and_me_works(client: Any) -> None:
    await _bootstrap_admin(client)
    pair = await _login(client, "admin1", "passw0rd!")
    assert "access_token" in pair and "refresh_token" in pair
    res = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
    )
    assert res.status_code == 200
    assert res.json()["username"] == "admin1"
    assert res.json()["role"] == "admin"


async def test_login_bad_password_returns_401(client: Any) -> None:
    await _bootstrap_admin(client)
    res = await client.post(
        "/api/v1/auth/login", json={"username": "admin1", "password": "WRONG-PASSWORD"}
    )
    assert res.status_code == 401
    assert res.json()["code"] == "bad_credentials"


async def test_refresh_rotates_token_and_revokes_old(client: Any, db_session: Any) -> None:
    await _bootstrap_admin(client)
    pair1 = await _login(client, "admin1", "passw0rd!")
    res = await client.post("/api/v1/auth/refresh", json={"refresh_token": pair1["refresh_token"]})
    assert res.status_code == 200
    pair2 = res.json()
    assert pair2["refresh_token"] != pair1["refresh_token"]

    # 旧 refresh 再用 → 401
    res2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": pair1["refresh_token"]})
    assert res2.status_code == 401
    assert res2.json()["code"] == "token_revoked"


async def test_logout_revokes_refresh_token(client: Any, db_session: Any) -> None:
    await _bootstrap_admin(client)
    pair = await _login(client, "admin1", "passw0rd!")
    res = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": pair["refresh_token"]},
        headers={"Authorization": f"Bearer {pair['access_token']}"},
    )
    assert res.status_code == 204
    # logout 后用旧 refresh → 401
    res2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert res2.status_code == 401
    assert res2.json()["code"] == "token_revoked"


async def test_me_without_token_returns_401(client: Any) -> None:
    res = await client.get("/api/v1/auth/me")
    assert res.status_code == 401


async def test_me_with_bad_token_returns_401(client: Any) -> None:
    res = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert res.status_code == 401


# === RBAC ===


async def test_normal_user_cannot_access_admin_users_route(client: Any) -> None:
    """403 — 普通用户访问 admin 路由。"""
    await _bootstrap_admin(client)
    admin_pair = await _login(client, "admin1", "passw0rd!")
    # admin 创建一个普通 user
    res = await client.post(
        "/api/v1/users",
        json={"username": "alice", "password": "alice1234", "role": "user"},
        headers={"Authorization": f"Bearer {admin_pair['access_token']}"},
    )
    assert res.status_code == 201
    alice_pair = await _login(client, "alice", "alice1234")
    # 普通用户访问 /users → 403
    res = await client.get(
        "/api/v1/users", headers={"Authorization": f"Bearer {alice_pair['access_token']}"}
    )
    assert res.status_code == 403
    assert res.json()["code"] == "forbidden"


async def test_admin_can_list_create_patch_users(client: Any) -> None:
    await _bootstrap_admin(client)
    admin_pair = await _login(client, "admin1", "passw0rd!")
    headers = {"Authorization": f"Bearer {admin_pair['access_token']}"}

    res = await client.post(
        "/api/v1/users",
        json={"username": "bob", "password": "bobpassword", "role": "user"},
        headers=headers,
    )
    assert res.status_code == 201
    bob_id = res.json()["id"]

    # list
    res = await client.get("/api/v1/users", headers=headers)
    assert res.status_code == 200
    items = res.json()["items"]
    usernames = {i["username"] for i in items}
    assert {"admin1", "bob"}.issubset(usernames)

    # patch — disable
    res = await client.patch(f"/api/v1/users/{bob_id}", json={"is_active": False}, headers=headers)
    assert res.status_code == 200
    assert res.json()["is_active"] is False


# === 停用用户无法 refresh ===


async def test_disabled_user_cannot_refresh(client: Any) -> None:
    await _bootstrap_admin(client)
    admin_pair = await _login(client, "admin1", "passw0rd!")
    headers = {"Authorization": f"Bearer {admin_pair['access_token']}"}
    # 创建 + 登录 bob
    res = await client.post(
        "/api/v1/users",
        json={"username": "bob", "password": "bobpassword"},
        headers=headers,
    )
    bob_id = res.json()["id"]
    bob_pair = await _login(client, "bob", "bobpassword")
    # admin 停用 bob → 应当 revoke 全部 refresh
    res = await client.patch(f"/api/v1/users/{bob_id}", json={"is_active": False}, headers=headers)
    assert res.status_code == 200

    # bob 旧 refresh 应当被 revoked
    res = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": bob_pair["refresh_token"]}
    )
    assert res.status_code == 401
    # 也不能用旧 access 调 me（user_inactive 路径）
    res = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {bob_pair['access_token']}"},
    )
    assert res.status_code == 401
    assert res.json()["code"] == "user_inactive"


# === 审计 ===


async def test_audit_logs_written_on_admin_actions(client: Any, db_session: Any) -> None:
    await _bootstrap_admin(client)
    admin_pair = await _login(client, "admin1", "passw0rd!")
    headers = {"Authorization": f"Bearer {admin_pair['access_token']}"}
    await client.post(
        "/api/v1/users",
        json={"username": "carol", "password": "carolpass"},
        headers=headers,
    )
    # 直接读 DB
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    actions = {r.action for r in rows}
    assert "bootstrap_admin.success" in actions
    assert "user.create" in actions


async def test_audit_does_not_contain_chat_content_in_m46(client: Any, db_session: Any) -> None:
    """M4.6 阶段不应有 chat 相关 audit；该断言为反例保护，防止未来误加。"""
    await _bootstrap_admin(client)
    await _login(client, "admin1", "passw0rd!")
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    for r in rows:
        assert "chat" not in r.action
        # extra metadata 不带 content / token
        assert "content" not in r.extra
        assert "token" not in r.extra


# === 限流 (chat bucket) — 通过 dependency 注入路由验证 ===


async def test_rate_limit_returns_429_when_bucket_exhausted(
    client: Any, app_and_state: Any
) -> None:
    """单独挂一个测试路由用 rate_limit("chat") 验证 429 行为。"""
    from fastapi import Depends

    from app.core.auth import get_current_user
    from app.core.ratelimit import BUCKETS, rate_limit
    from app.db.models import User as UserModel

    app, _, _ = app_and_state

    @app.get("/api/v1/__rl_test", dependencies=[Depends(rate_limit("chat"))])
    async def _rl_test(user: UserModel = Depends(get_current_user)) -> dict[str, str]:
        return {"user": user.username}

    await _bootstrap_admin(client)
    pair = await _login(client, "admin1", "passw0rd!")
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    limit = BUCKETS["chat"].limit
    for _ in range(limit):
        res = await client.get("/api/v1/__rl_test", headers=headers)
        assert res.status_code == 200
    # 超出 → 429
    res = await client.get("/api/v1/__rl_test", headers=headers)
    assert res.status_code == 429
    assert res.json()["code"] == "rate_limited"


async def test_refresh_tokens_table_state_after_full_flow(client: Any, db_session: Any) -> None:
    """完整 flow 后 DB 状态：login + refresh + logout 应留下 2 条 RefreshToken，全部 revoked。"""
    await _bootstrap_admin(client)
    pair1 = await _login(client, "admin1", "passw0rd!")
    res = await client.post("/api/v1/auth/refresh", json={"refresh_token": pair1["refresh_token"]})
    pair2 = res.json()
    res = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": pair2["refresh_token"]},
        headers={"Authorization": f"Bearer {pair2['access_token']}"},
    )
    assert res.status_code == 204

    rows = (await db_session.execute(select(RefreshToken))).scalars().all()
    # 2 条都被 revoked（pair1 rotation 被 revoke；pair2 logout 被 revoke）
    assert len(rows) == 2
    assert all(r.revoked_at is not None for r in rows)


async def test_users_table_only_one_after_bootstrap(client: Any, db_session: Any) -> None:
    await _bootstrap_admin(client)
    rows = (await db_session.execute(select(User))).scalars().all()
    assert len(rows) == 1
    assert rows[0].role == "admin"
