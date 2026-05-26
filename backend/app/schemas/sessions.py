"""Pydantic v2 schemas for /sessions CRUD（M4.7）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# raw_lookup 模式已下线，仅保留 qa。dev DB 历史行可能仍是 'raw_lookup'，读出时
# 由 SessionOut.mode_default 的 before-validator 归一成 'qa'，不做 schema migration。
Mode = Literal["qa"]
SessionStatus = Literal["active", "paused", "archived_branch"]


class SessionCreateBody(BaseModel):
    title: str = Field(default="", max_length=255)
    mode_default: Mode = "qa"


class SessionPatchBody(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    mode_default: Mode | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    mode_default: Mode
    status: SessionStatus
    forked_from_session_id: uuid.UUID | None
    forked_from_checkpoint_id: str | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator("mode_default", mode="before")
    @classmethod
    def _coerce_legacy_mode(cls, v: object) -> str:
        # 历史 'raw_lookup' 等非 qa 值归一为 'qa'，避免老会话列表读取时 ValidationError。
        return "qa"


class SessionListResponse(BaseModel):
    items: list[SessionOut]
    total: int
