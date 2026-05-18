"""M4.9 集成测：`/api/v1/notes` CRUD。"""

from __future__ import annotations

from typing import Any

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
