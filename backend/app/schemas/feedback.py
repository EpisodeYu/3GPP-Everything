"""Pydantic v2 schemas for /messages/{mid}/feedback（M4.9）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FeedbackBody(BaseModel):
    thumb: Literal[1, -1]
    reason: str | None = Field(default=None, max_length=2000)


class FeedbackOut(BaseModel):
    id: uuid.UUID
    message_id: uuid.UUID
    thumb: int
    reason: str | None
    created_at: datetime
