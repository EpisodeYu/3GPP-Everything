"""M4.9 集成测：`/api/v1/favorites` CRUD + 跨用户隔离。"""

from __future__ import annotations

from typing import Any

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
