"""PgChunkMetaWriter 单测（用 in-memory SQLite，验证 schema + DELETE-then-INSERT）。

覆盖：
- ensure_schema 建表 idempotent
- upsert_chunks 写入 + count
- 同 chunk_id + provider 重写覆盖（不重复）
- 同 chunk_id 不同 provider 共存（双轨）
- purge_spec 按 spec_id + provider 删
- _to_sync_psycopg 把 asyncpg dsn 转为 psycopg dsn
- chunks_to_rows 辅助 round-trip
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select

from ingestion.indexer.pg_writer import (
    PgChunkMetaWriter,
    _to_sync_psycopg,
    chunks_meta_table,
    chunks_to_rows,
)


@dataclass(slots=True)
class _Chunk:
    chunk_id: str
    spec_id: str = "38.211"
    spec_uid: str | None = "38211"
    spec_number: str = "38.211"
    spec_type: str = "TS"
    release: str = "Rel-19"
    series: str = "38"
    title: str = "NR; Physical channels"
    chunk_type: str = "text"
    clause: str = "5.2.1"
    section_path: tuple[str, ...] = ("5", "2", "1")
    section_title: str = "Section"
    parent_section_id: str = "p1"
    parent_section_chars: int = 1000
    document_order: int = 0
    content: str = "Some content."
    raw_extra: dict = field(default_factory=dict)
    cross_refs: list[str] = field(default_factory=list)
    source: str = "gsma_hf"
    source_version: str = "rev1"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _mk(n: int, *, spec_id: str = "38.211", suffix: str = "") -> list[_Chunk]:
    return [_Chunk(chunk_id=f"{spec_id}-{i}{suffix}", spec_id=spec_id) for i in range(n)]


def _writer(provider: str = "voyage") -> PgChunkMetaWriter:
    engine = create_engine("sqlite:///:memory:", future=True)
    return PgChunkMetaWriter(engine=engine, provider=provider)


def test_to_sync_psycopg_converts_asyncpg() -> None:
    assert (
        _to_sync_psycopg("postgresql+asyncpg://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    )
    # 已是 psycopg → 不变
    assert (
        _to_sync_psycopg("postgresql+psycopg://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    )
    # sqlite 不变
    assert _to_sync_psycopg("sqlite:///x.db") == "sqlite:///x.db"


def test_ensure_schema_idempotent() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    w = PgChunkMetaWriter(engine=engine, provider="voyage")
    # 再次手动调 ensure_schema 不应抛
    w.ensure_schema()
    w.ensure_schema()


def test_upsert_and_count() -> None:
    w = _writer()
    chunks = _mk(5)
    n = w.upsert_chunks(chunks)
    assert n == 5
    assert w.count() == 5
    assert w.count(spec_id="38.211") == 5
    assert w.count(spec_id="other") == 0


def test_upsert_same_chunk_id_same_provider_replaces() -> None:
    w = _writer()
    chunks = _mk(3)
    w.upsert_chunks(chunks)
    assert w.count() == 3
    # 同 chunk_id 再写一次 → 覆盖不重复
    w.upsert_chunks(chunks)
    assert w.count() == 3


def test_dual_provider_same_chunk_id_coexists() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    w1 = PgChunkMetaWriter(engine=engine, provider="voyage")
    w2 = PgChunkMetaWriter(engine=engine, provider="glm", schema_owner=False)
    chunks = _mk(3)
    w1.upsert_chunks(chunks)
    w2.upsert_chunks(chunks)
    assert w1.count() == 3
    assert w2.count() == 3
    # 总行数 = 6
    with engine.connect() as conn:
        rows = list(conn.execute(select(chunks_meta_table)))
    assert len(rows) == 6


def test_purge_spec_removes_provider_scoped() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    w1 = PgChunkMetaWriter(engine=engine, provider="voyage")
    w2 = PgChunkMetaWriter(engine=engine, provider="glm", schema_owner=False)
    w1.upsert_chunks(_mk(3, spec_id="A.A"))
    w1.upsert_chunks(_mk(2, spec_id="B.B"))
    w2.upsert_chunks(_mk(3, spec_id="A.A"))
    removed = w1.purge_spec("A.A")
    assert removed == 3
    assert w1.count(spec_id="A.A") == 0
    assert w1.count(spec_id="B.B") == 2
    # glm 那份没被动
    assert w2.count(spec_id="A.A") == 3


def test_raw_extra_round_trip() -> None:
    w = _writer()
    c = _Chunk(
        chunk_id="x-1",
        raw_extra={"image_path": "img.jpg", "vision": {"figure_kind": "logo"}},
        cross_refs=["foo"],
        section_path=("5", "2", "1"),
    )
    w.upsert_chunks([c])
    with w._engine.connect() as conn:
        row = conn.execute(select(chunks_meta_table)).first()
    assert row is not None
    # raw_extra / section_path / cross_refs round-trip
    assert row.raw_extra["image_path"] == "img.jpg"
    assert row.raw_extra["vision"] == {"figure_kind": "logo"}
    assert row.cross_refs == ["foo"]
    assert row.section_path == ["5", "2", "1"]


def test_chunks_to_rows_helper() -> None:
    rows = chunks_to_rows(_mk(2), provider="voyage")
    assert len(rows) == 2
    assert rows[0]["chunk_id"] == "38.211-0"
    assert rows[0]["provider"] == "voyage"
    assert rows[0]["char_offset_start"] is None
    assert rows[0]["char_offset_end"] is None


def test_upsert_empty_returns_zero() -> None:
    w = _writer()
    assert w.upsert_chunks([]) == 0


def test_purge_missing_spec_returns_zero() -> None:
    w = _writer()
    assert w.purge_spec("never-existed") == 0


def test_from_env_missing_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        PgChunkMetaWriter.from_env(provider="voyage")
