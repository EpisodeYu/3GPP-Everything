"""Pydantic v2 schemas for /sessions/{sid}/messages（F-5 / F-6）。

`MessageOut` 字段对齐 `db.models.Message` + `MessageCitation` 派生的 citations 列表，
满足 §2 路由总表 "消息列表 + 详情（含 citations）" 口径。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["user", "assistant", "system"]
MessageStatus = Literal["ok", "cancelled", "failed"]


class MessageCitationOut(BaseModel):
    chunk_id: str
    rank: int
    spec_id: str
    section_path: str
    rerank_score: float | None = None
    char_offset_start: int | None = None
    char_offset_end: int | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    role: Role
    content: str
    status: MessageStatus
    mode: str | None = None
    explicit_tools: list[str] = Field(default_factory=list)
    confidence: float | None = None
    self_rag_verdict: str | None = None
    langgraph_run_id: str | None = None
    created_at: datetime
    citations: list[MessageCitationOut] = Field(default_factory=list)


class MessageListResponse(BaseModel):
    items: list[MessageOut]
    total: int
