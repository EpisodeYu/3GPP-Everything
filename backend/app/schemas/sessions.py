"""Pydantic v2 schemas for /sessions CRUD（M4.7）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Mode = Literal["qa", "raw_lookup"]
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


class SessionListResponse(BaseModel):
    items: list[SessionOut]
    total: int
