"""Pydantic v2 schemas for /notes（M4.9）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TargetType = Literal["chunk", "message"]


class NoteCreateBody(BaseModel):
    target_type: TargetType
    target_id: str = Field(min_length=1, max_length=128)
    body: str = Field(default="", max_length=8000)


class NotePatchBody(BaseModel):
    body: str = Field(min_length=0, max_length=8000)


class NoteOut(BaseModel):
    id: uuid.UUID
    target_type: TargetType
    target_id: str
    body: str
    created_at: datetime
    updated_at: datetime


class NoteListResponse(BaseModel):
    items: list[NoteOut]
