"""SQLAlchemy 2.0 ORM 模型集（PG-primary，SQLite-friendly）。

口径 = `docs/03-development/04-backend-api.md §3.1`。

实务偏离（M4.0 已记录在交付报告）：
- `chunks_meta` 与 ingestion 现状对齐：**无** `document_id` FK；保留 `spec_id` /
  `spec_number` 软关联。`documents` 表此阶段空表（M4.9 reader / M8 crawler 接入后回填）。
- 跨方言：UUID 用 `String(36)` + `Uuid` SQLAlchemy 2.0 native（asPG 端用 UUID 列）；
  ARRAY 用 `JSON` 存（避免 SQLite 不支持 ARRAY）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from .base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


# === auth ===


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(128))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


# === sessions / messages ===


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    mode_default: Mapped[str] = mapped_column(String(16), nullable=False, default="qa")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    forked_from_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL")
    )
    forked_from_checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # M4.7 Q9: 仅 final event 后写 content；中断 → status='failed'/'cancelled'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    user_language: Mapped[str | None] = mapped_column(String(8))
    mode: Mapped[str | None] = mapped_column(String(16))
    explicit_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float | None] = mapped_column(Float)
    self_rag_verdict: Mapped[str | None] = mapped_column(String(32))
    langgraph_run_id: Mapped[str | None] = mapped_column(String(64))
    # DEPRECATED（2026-06-02）：系统不维护 message↔checkpoint 映射。真相源是 PG，
    # 每轮 agent 历史从 PG 重建；fork/rollback 的用户可见精度落在 messages 行级别，
    # 不依赖 LangGraph checkpoint id。旧实现误把 trace_id 写进此列（= langfuse_trace_id），
    # 已停写。列暂保留（删列属不向后兼容 schema 改动，需单独 migration + 人审）；新写入恒 NULL。
    langgraph_checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    citations: Mapped[list[MessageCitation]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
    )


class MessageCitation(Base):
    __tablename__ = "message_citations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # chunks_meta 的 PK 是 Integer（ingestion 现状对齐，见模块顶部 docstring）
    chunk_meta_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("chunks_meta.id", ondelete="SET NULL"),
    )
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rerank_score: Mapped[float | None] = mapped_column(Float)
    spec_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    section_path: Mapped[str] = mapped_column(String(64), nullable=False)
    char_offset_start: Mapped[int | None] = mapped_column(Integer)
    char_offset_end: Mapped[int | None] = mapped_column(Integer)

    message: Mapped[Message] = relationship(back_populates="citations")


# === documents / chunks ===


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("spec_id", "release", name="uq_documents_spec_id_release"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    spec_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    spec_uid: Mapped[str | None] = mapped_column(String(32))
    release: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    series: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    latest_version: Mapped[str | None] = mapped_column(String(32))
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_msg: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="gsma_hf")
    gsma_dataset_revision: Mapped[str | None] = mapped_column(String(64))


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    downloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_for_providers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class ChunkMeta(Base):
    """与 ingestion 端 ingestion/indexer/pg_writer.chunks_meta_table 字段对齐。

    backend ORM 不直接写这张表（ingestion 是唯一写者），仅做只读关联 + alembic 管理 schema。
    """

    __tablename__ = "chunks_meta"
    __table_args__ = (
        UniqueConstraint("chunk_id", "provider", name="uq_chunks_meta_chunk_id_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    spec_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    spec_uid: Mapped[str | None] = mapped_column(String(32))
    spec_number: Mapped[str] = mapped_column(String(32), nullable=False)
    spec_type: Mapped[str] = mapped_column(String(8), nullable=False)
    release: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    series: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    clause: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    section_path: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    section_title: Mapped[str] = mapped_column(Text, nullable=False)
    parent_section_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parent_section_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    document_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_offset_start: Mapped[int | None] = mapped_column(Integer)
    char_offset_end: Mapped[int | None] = mapped_column(Integer)
    raw_extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cross_refs: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# === glossary / favorites / notes / feedback ===


class Glossary(Base):
    __tablename__ = "glossary"

    id: Mapped[uuid.UUID] = _uuid_pk()
    term: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_term: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    spec_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    section_path: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_chunk_meta_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("chunks_meta.id", ondelete="SET NULL"),
    )
    source_revision: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Favorite(Base):
    __tablename__ = "favorites"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Feedback(Base):
    __tablename__ = "feedbacks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    thumb: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# === metrics / tasks ===


class ApiUsage(Base):
    __tablename__ = "api_usage"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_api_usage_user_id_day"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day: Mapped[Any] = mapped_column(Date, nullable=False)
    llm_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    embedding_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rerank_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    web_search_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    log_tail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
