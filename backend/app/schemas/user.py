"""User admin schemas（/api/v1/users 路由）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

Role = Literal["user", "admin"]


class UserCreateBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=8)
    role: Role = "user"


class UserPatchBody(BaseModel):
    is_active: bool | None = None
    role: Role | None = None
    password: SecretStr | None = Field(default=None, min_length=8)


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserListResponse(BaseModel):
    items: list[UserOut]
    total: int
