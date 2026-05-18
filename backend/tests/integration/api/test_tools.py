"""M4.9 集成测：`/api/v1/tools/glossary/search` + `/api/v1/tools/toc`。"""

from __future__ import annotations

from typing import Any

from app.db.models import ChunkMeta, Glossary

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


async def _seed_glossary(db_session: Any) -> None:
    db_session.add_all(
        [
            Glossary(
                term="AMF",
                normalized_term="amf",
                definition="Access and Mobility Management Function.",
                spec_id="23.501",
                section_path=["6", "3", "1"],
            ),
            Glossary(
                term="SMF",
                normalized_term="smf",
                definition="Session Management Function.",
                spec_id="23.501",
                section_path=["6", "3", "2"],
            ),
        ]
    )
    await db_session.commit()


async def _seed_toc_chunks(db_session: Any) -> None:
    db_session.add_all(
        [
            ChunkMeta(
                chunk_id="c-a",
                provider="voyage",
                spec_id="38.331",
                spec_uid=None,
                spec_number="38331",
                spec_type="TS",
                release="Rel-17",
                series="38",
                title="NR RRC",
                chunk_type="text",
                clause="5.3",
                section_path=["5", "3"],
                section_title="RRC connection est.",
                parent_section_id="5.3",
                parent_section_chars=100,
                document_order=10,
                char_offset_start=0,
                char_offset_end=10,
                raw_extra={},
                cross_refs=[],
                source="gsma_hf",
                source_version="v1",
            ),
            ChunkMeta(
                chunk_id="c-b",
                provider="voyage",
                spec_id="38.331",
                spec_uid=None,
                spec_number="38331",
                spec_type="TS",
                release="Rel-17",
                series="38",
                title="NR RRC",
                chunk_type="text",
                clause="5.3.5",
                section_path=["5", "3", "5"],
                section_title="RRC reconfig",
                parent_section_id="5.3.5",
                parent_section_chars=100,
                document_order=11,
                char_offset_start=0,
                char_offset_end=10,
                raw_extra={},
                cross_refs=[],
                source="gsma_hf",
                source_version="v1",
            ),
        ]
    )
    await db_session.commit()


async def test_glossary_search_hits(client: Any, db_session: Any) -> None:
    await _seed_glossary(db_session)
    token = await _new_user_token(client)
    r = await client.post(
        "/api/v1/tools/glossary/search",
        json={"term": "AMF"},
        headers=_auth_headers(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["term"] == "AMF"


async def test_glossary_search_misses(client: Any, db_session: Any) -> None:
    await _seed_glossary(db_session)
    token = await _new_user_token(client)
    r = await client.post(
        "/api/v1/tools/glossary/search",
        json={"term": "NOPE"},
        headers=_auth_headers(token),
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


async def test_toc_returns_sections_under_prefix(client: Any, db_session: Any) -> None:
    await _seed_toc_chunks(db_session)
    token = await _new_user_token(client)
    r = await client.post(
        "/api/v1/tools/toc",
        json={"spec_id": "38.331", "section_prefix": ["5", "3"]},
        headers=_auth_headers(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spec_id"] == "38.331"
    paths = [it["section_path"] for it in body["items"]]
    assert ["5", "3"] in paths
    assert ["5", "3", "5"] in paths


async def test_tools_require_auth(client: Any) -> None:
    r = await client.post("/api/v1/tools/glossary/search", json={"term": "AMF"})
    assert r.status_code == 401
