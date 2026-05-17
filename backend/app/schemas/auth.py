"""鉴权相关 Pydantic v2 schemas。

口径锚 `docs/03-development/04-backend-api.md §2 / §5`。
密码策略 Q3：min_length=8，不强制字符复杂度。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class BootstrapAdminBody(BaseModel):
    """`POST /auth/bootstrap-admin`：仅在 users 表为空时可用。"""

    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=8)
    invite_code: SecretStr


class LoginBody(BaseModel):
    """`POST /auth/login`。"""

    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1)  # 登录侧不再 enforce 8 长度（用户改密前的历史账号）


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=10)


class LogoutBody(BaseModel):
    refresh_token: str = Field(min_length=10)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # access token 秒数


class MeResponse(BaseModel):
    id: uuid.UUID
    username: str
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
