"""ORM 元数据 sanity：所有表注册、列必要约束、FK 指向正确。

不连真实 DB；只检查 SQLAlchemy metadata 自身（M4.0 之后由 alembic upgrade 在
clean PG 上做 schema-level 验证）。
"""

from __future__ import annotations

from sqlalchemy import Integer
from sqlalchemy.types import Uuid

from app.db import Base

EXPECTED_TABLES = {
    "users",
    "refresh_tokens",
    "audit_logs",
    "sessions",
    "messages",
    "message_citations",
    "documents",
    "document_versions",
    "chunks_meta",
    "glossary",
    "favorites",
    "notes",
    "feedbacks",
    "api_usage",
    "tasks",
}


def test_all_tables_registered() -> None:
    assert EXPECTED_TABLES.issubset(Base.metadata.tables.keys())


def test_chunks_meta_pk_is_integer() -> None:
    """与 ingestion/indexer/pg_writer.chunks_meta_table 一致（Integer 而非 UUID）。"""
    cm = Base.metadata.tables["chunks_meta"]
    pk = list(cm.primary_key.columns)
    assert len(pk) == 1
    assert isinstance(pk[0].type, Integer)


def test_message_citations_fk_to_chunks_meta_integer() -> None:
    """citation.chunk_meta_id 指向 chunks_meta.id (Integer)，不是 UUID。"""
    mc = Base.metadata.tables["message_citations"]
    col = mc.c.chunk_meta_id
    assert isinstance(col.type, Integer)
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "chunks_meta"


def test_glossary_source_fk_to_chunks_meta_integer() -> None:
    g = Base.metadata.tables["glossary"]
    col = g.c.source_chunk_meta_id
    assert isinstance(col.type, Integer)


def test_users_id_uuid_pk() -> None:
    users = Base.metadata.tables["users"]
    pk = list(users.primary_key.columns)
    assert len(pk) == 1
    assert isinstance(pk[0].type, Uuid)


def test_feedbacks_message_id_unique() -> None:
    fb = Base.metadata.tables["feedbacks"]
    assert any(c.name == "message_id" and c.unique for c in fb.columns)


def test_api_usage_unique_user_id_day() -> None:
    au = Base.metadata.tables["api_usage"]
    uqs = [c for c in au.constraints if c.__class__.__name__ == "UniqueConstraint"]
    cols_sets = [{col.name for col in c.columns} for c in uqs]
    assert {"user_id", "day"} in cols_sets


def test_sessions_self_fk_for_fork() -> None:
    s = Base.metadata.tables["sessions"]
    col = s.c.forked_from_session_id
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "sessions"
