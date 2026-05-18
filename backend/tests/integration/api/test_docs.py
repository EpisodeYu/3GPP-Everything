"""M4.9 集成测：`/api/v1/docs` Reader + `/api/v1/chunks/{chunk_id}` 单 chunk。

文档锚 04-backend-api.md §M4.9。
"""

from __future__ import annotations

from typing import Any

from app.db.models import ChunkMeta

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


def _make_chunk(
    *,
    chunk_id: str,
    spec_id: str = "23.501",
    section_path: list[str] | None = None,
    section_title: str = "AMF",
    chunk_type: str = "text",
    release: str = "Rel-18",
    series: str = "23",
    title: str = "5G System; Stage 2",
    content: str = "AMF stands for Access and Mobility Management Function.",
    document_order: int = 1,
    clause: str | None = None,
    provider: str = "voyage",
) -> ChunkMeta:
    section_path = section_path or ["6", "3", "1"]
    return ChunkMeta(
        chunk_id=chunk_id,
        provider=provider,
        spec_id=spec_id,
        spec_uid=None,
        spec_number=spec_id.replace(".", ""),
        spec_type="TS",
        release=release,
        series=series,
        title=title,
        chunk_type=chunk_type,
        clause=clause or ".".join(section_path),
        section_path=section_path,
        section_title=section_title,
        parent_section_id=".".join(section_path),
        parent_section_chars=200,
        document_order=document_order,
        char_offset_start=0,
        char_offset_end=len(content),
        raw_extra={"content": content},
        cross_refs=[],
        source="gsma_hf",
        source_version="v1",
    )


async def _seed_chunks(db_session: Any) -> None:
    db_session.add_all(
        [
            _make_chunk(chunk_id="c1", document_order=1),
            _make_chunk(
                chunk_id="c2",
                section_path=["6", "3", "2"],
                section_title="SMF",
                content="SMF Session Management Function.",
                document_order=2,
            ),
            _make_chunk(
                chunk_id="c3",
                spec_id="38.331",
                series="38",
                title="NR; RRC",
                section_path=["5", "3"],
                section_title="RRC connection establishment",
                content="RRC procedure description.",
                release="Rel-17",
                document_order=1,
            ),
        ]
    )
    await db_session.commit()


async def test_list_docs_groups_by_spec_id(client: Any, db_session: Any) -> None:
    await _seed_chunks(db_session)
    token = await _new_user_token(client)
    r = await client.get("/api/v1/docs", headers=_auth_headers(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    specs = {it["spec_id"] for it in body["items"]}
    assert specs == {"23.501", "38.331"}
    # 23.501 有 2 个 chunk
    s23 = next(it for it in body["items"] if it["spec_id"] == "23.501")
    assert s23["chunk_count"] == 2


async def test_list_docs_filters_by_release_and_series(client: Any, db_session: Any) -> None:
    await _seed_chunks(db_session)
    token = await _new_user_token(client)
    r = await client.get("/api/v1/docs?release=Rel-17&series=38", headers=_auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["spec_id"] == "38.331"


async def test_get_doc_returns_section_tree(client: Any, db_session: Any) -> None:
    await _seed_chunks(db_session)
    token = await _new_user_token(client)
    r = await client.get("/api/v1/docs/23.501", headers=_auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert body["spec_id"] == "23.501"
    assert body["release"] == "Rel-18"
    paths = [s["section_path"] for s in body["sections"]]
    assert ["6", "3", "1"] in paths
    assert ["6", "3", "2"] in paths


async def test_get_doc_404(client: Any, db_session: Any) -> None:
    await _seed_chunks(db_session)
    token = await _new_user_token(client)
    r = await client.get("/api/v1/docs/99.999", headers=_auth_headers(token))
    assert r.status_code == 404
    assert r.json()["code"] == "doc_not_found"


async def test_get_section_returns_chunks(client: Any, db_session: Any) -> None:
    await _seed_chunks(db_session)
    token = await _new_user_token(client)
    r = await client.get("/api/v1/docs/23.501/sections/6.3.1", headers=_auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert body["section_path"] == ["6", "3", "1"]
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["chunk_id"] == "c1"
    assert "Access and Mobility" in body["chunks"][0]["content"]


async def test_search_in_doc(client: Any, db_session: Any) -> None:
    await _seed_chunks(db_session)
    token = await _new_user_token(client)
    r = await client.get("/api/v1/docs/23.501/search?q=SMF", headers=_auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "SMF"
    assert any(it["chunk_id"] == "c2" for it in body["items"])


async def test_get_chunk_by_id(client: Any, db_session: Any) -> None:
    await _seed_chunks(db_session)
    token = await _new_user_token(client)
    r = await client.get("/api/v1/chunks/c2", headers=_auth_headers(token))
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_id"] == "c2"
    assert body["spec_id"] == "23.501"
    assert body["section_title"] == "SMF"


async def test_get_chunk_404(client: Any) -> None:
    token = await _new_user_token(client)
    r = await client.get("/api/v1/chunks/does-not-exist", headers=_auth_headers(token))
    assert r.status_code == 404
    assert r.json()["code"] == "chunk_not_found"


async def test_docs_routes_require_auth(client: Any) -> None:
    r = await client.get("/api/v1/docs")
    assert r.status_code == 401
    r = await client.get("/api/v1/chunks/x")
    assert r.status_code == 401
