"""Pydantic v2 schemas for /favorites（M4.9）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TargetType = Literal["chunk", "message"]


class FavoriteCreateBody(BaseModel):
    target_type: TargetType
    target_id: str = Field(min_length=1, max_length=128)


class FavoriteOut(BaseModel):
    id: uuid.UUID
    target_type: TargetType
    target_id: str
    created_at: datetime


class FavoriteListResponse(BaseModel):
    items: list[FavoriteOut]
