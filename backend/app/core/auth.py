"""鉴权 / 授权底座：密码哈希、JWT 签发与校验、FastAPI Depends。

文档锚点：`docs/03-development/04-backend-api.md §5`。

设计要点：
- 密码：bcrypt 直接调用（passlib 1.7.4 已不维护，且在 Python 3.13 / bcrypt 5 环境下
  触发 `crypt` 模块 DeprecationWarning，不可靠）。
- JWT：HS256，secret 从 `settings.APP_SECRET_KEY` 读；access 15min / refresh 7d 默认。
  payload 字段：`sub`(user_id) / `role` / `type`("access"|"refresh") / `jti` / `exp` / `iat`。
- refresh token：DB 落 hash（SHA-256 take 32 hex），logout / 用户停用 / 密码重置时
  把 `revoked_at` 置为 now。
- FastAPI Depends：
  - `get_current_user`：解析 access token；校验 active；DB 缺失 → 401。
  - `require_role("admin", ...)`：基于 `current_user.role` 做 RBAC，失败 → 403。

安全约束：
- token 不能写日志原文；本模块异常 message 只透 code，不带 token。
- bcrypt cost 用 bcrypt 默认（12 rounds，2025 年仍是合理基线）。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.db.base import get_db
from app.db.models import User

JWT_ALGORITHM = "HS256"
TokenType = Literal["access", "refresh"]


# === 密码 ===


def hash_password(password: str) -> str:
    """bcrypt 哈希，返回 utf-8 字符串（DB 列是 String(255)）。"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """常量时间比较，密码错误返回 False（不抛）。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # 哈希被人手改坏 / 历史脏数据：当作密码错误，避免 500
        return False


# === JWT ===


def _settings_or(s: Settings | None) -> Settings:
    return s or get_settings()


def _secret(s: Settings) -> str:
    raw = s.APP_SECRET_KEY.get_secret_value()
    if not raw:
        # 生产部署忘记设置 secret 直接挂掉；dev 默认值在 .env 应填写
        raise UnauthorizedError(
            "APP_SECRET_KEY not configured",
            code="server_misconfigured",
            status_code=500,
        )
    return raw


def create_access_token(
    *,
    user_id: uuid.UUID,
    role: str,
    settings: Settings | None = None,
    jti: str | None = None,
) -> str:
    s = _settings_or(settings)
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "jti": jti or str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, _secret(s), algorithm=JWT_ALGORITHM)


def create_refresh_token(
    *,
    user_id: uuid.UUID,
    role: str,
    settings: Settings | None = None,
    jti: str | None = None,
) -> tuple[str, str, datetime]:
    """返回 (token, token_hash, expires_at)。token_hash 入 DB；token 给客户端。"""
    s = _settings_or(settings)
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=s.REFRESH_TOKEN_EXPIRE_DAYS)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": "refresh",
        "jti": jti or str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, _secret(s), algorithm=JWT_ALGORITHM)
    return token, hash_refresh_token(token), expires_at


def hash_refresh_token(token: str) -> str:
    """refresh token 用 SHA-256 hex 入库，作为 token_hash 列内容。

    不用 bcrypt：refresh 校验路径 hot path，需要 O(1) 查表；token 本身已是高熵 JWT，
    SHA-256 足够。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decode_token(token: str, *, settings: Settings | None = None) -> dict[str, Any]:
    s = _settings_or(settings)
    try:
        return jwt.decode(token, _secret(s), algorithms=[JWT_ALGORITHM])
    except JWTError as e:
        raise UnauthorizedError("invalid_token", code="invalid_token") from e


def require_token_type(payload: dict[str, Any], expected: TokenType) -> None:
    if payload.get("type") != expected:
        raise UnauthorizedError("wrong_token_type", code="wrong_token_type")


# === FastAPI Depends ===

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    if creds is None or not creds.credentials:
        raise UnauthorizedError("missing_token", code="missing_token")
    payload = decode_token(creds.credentials, settings=settings)
    require_token_type(payload, "access")
    sub = payload.get("sub")
    if not sub:
        raise UnauthorizedError("invalid_token", code="invalid_token")
    try:
        user_id = uuid.UUID(sub)
    except (TypeError, ValueError) as e:
        raise UnauthorizedError("invalid_token", code="invalid_token") from e
    res = await db.execute(select(User).where(User.id == user_id))
    user = res.scalar_one_or_none()
    if user is None or not user.is_active:
        # 用户被删/停用：拒绝继续
        raise UnauthorizedError("user_inactive", code="user_inactive")
    return user


def require_role(*roles: str):
    """生成 Depends：要求 current_user.role 命中 roles 之一，否则 403。"""

    async def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise ForbiddenError("role_required", code="forbidden", details={"need": list(roles)})
        return user

    return _dep
