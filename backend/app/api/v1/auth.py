"""`/api/v1/auth/*` 路由：bootstrap-admin / login / refresh / logout / me。

文档锚点：`docs/03-development/04-backend-api.md §2 / §5`。

行为约定：
- bootstrap-admin：仅在 users 表为空时返回 200；否则 409 Conflict。invite_code 校验失败 → 401。
- login：用户名 + 密码 → 签发 access + refresh；refresh hash 入库。
- refresh：传 refresh_token；查 token_hash + 未撤销 + 未过期 + 用户 is_active；
  签发**新** access + **新** refresh，并把旧 refresh 标 revoked（rotation 模型）。
- logout：撤销当前 refresh token（同 hash 在 DB 找到则 set revoked_at=now）。
- me：返回当前 access user 信息。

审计点（写 audit_logs，Q5 不包含 chat 内容）：
- bootstrap_admin.success / bootstrap_admin.failed
- user.login.failed（用户名错或密码错 / 账号停用），login.success 不写以免膨胀
- user.logout
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    hash_refresh_token,
    require_token_type,
    verify_password,
)
from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, UnauthorizedError
from app.db.base import get_db
from app.db.models import RefreshToken, User
from app.schemas.auth import (
    BootstrapAdminBody,
    LoginBody,
    LogoutBody,
    MeResponse,
    RefreshBody,
    TokenPair,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua


async def _issue_token_pair(db: AsyncSession, *, user: User, settings: Settings) -> TokenPair:
    access = create_access_token(user_id=user.id, role=user.role, settings=settings)
    refresh, refresh_hash, refresh_exp = create_refresh_token(
        user_id=user.id, role=user.role, settings=settings
    )
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=refresh_exp,
        )
    )
    await db.flush()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/bootstrap-admin", status_code=status.HTTP_201_CREATED, response_model=MeResponse)
async def bootstrap_admin(
    body: BootstrapAdminBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MeResponse:
    """首次部署：当 users 表为空时创建第一个管理员。invite_code 必须匹配。"""
    ip, ua = _client_meta(request)
    expected = settings.BOOTSTRAP_ADMIN_INVITE_CODE.get_secret_value()
    if not expected or body.invite_code.get_secret_value() != expected:
        await write_audit(
            db,
            actor_user_id=None,
            action="bootstrap_admin.failed",
            target_type="user",
            target_id=body.username,
            ip=ip,
            user_agent=ua,
            extra={"reason": "invalid_invite_code"},
        )
        await db.commit()
        raise UnauthorizedError("invalid_invite_code", code="invalid_invite_code")

    # users 表必须为空
    count_res = await db.execute(select(func.count()).select_from(User))
    count = count_res.scalar_one()
    if count > 0:
        await write_audit(
            db,
            actor_user_id=None,
            action="bootstrap_admin.failed",
            target_type="user",
            target_id=body.username,
            ip=ip,
            user_agent=ua,
            extra={"reason": "already_initialized"},
        )
        await db.commit()
        raise ConflictError("already_initialized", code="already_initialized")

    user = User(
        username=body.username,
        password_hash=hash_password(body.password.get_secret_value()),
        role="admin",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    await write_audit(
        db,
        actor_user_id=user.id,
        action="bootstrap_admin.success",
        target_type="user",
        target_id=str(user.id),
        ip=ip,
        user_agent=ua,
    )
    await db.commit()
    await db.refresh(user)
    return MeResponse.model_validate(user, from_attributes=True)


@router.post("/login", response_model=TokenPair)
async def login(
    body: LoginBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenPair:
    ip, ua = _client_meta(request)
    res = await db.execute(select(User).where(User.username == body.username))
    user = res.scalar_one_or_none()
    if user is None or not verify_password(body.password.get_secret_value(), user.password_hash):
        await write_audit(
            db,
            actor_user_id=user.id if user is not None else None,
            action="user.login.failed",
            target_type="user",
            target_id=body.username,
            ip=ip,
            user_agent=ua,
            extra={"reason": "bad_credentials"},
        )
        await db.commit()
        raise UnauthorizedError("bad_credentials", code="bad_credentials")
    if not user.is_active:
        await write_audit(
            db,
            actor_user_id=user.id,
            action="user.login.failed",
            target_type="user",
            target_id=str(user.id),
            ip=ip,
            user_agent=ua,
            extra={"reason": "user_inactive"},
        )
        await db.commit()
        raise UnauthorizedError("user_inactive", code="user_inactive")

    tokens = await _issue_token_pair(db, user=user, settings=settings)
    user.last_login_at = datetime.now(UTC)
    await db.commit()
    return tokens


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshBody,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenPair:
    payload = decode_token(body.refresh_token, settings=settings)
    require_token_type(payload, "refresh")
    sub = payload.get("sub")
    if not sub:
        raise UnauthorizedError("invalid_token", code="invalid_token")
    try:
        user_id = uuid.UUID(sub)
    except (TypeError, ValueError) as e:
        raise UnauthorizedError("invalid_token", code="invalid_token") from e

    token_hash = hash_refresh_token(body.refresh_token)
    res = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.user_id == user_id,
        )
    )
    rt = res.scalar_one_or_none()
    now = datetime.now(UTC)
    if rt is None or rt.revoked_at is not None:
        raise UnauthorizedError("token_revoked", code="token_revoked")
    # expires_at 来自 PG，带 tz；保险起见用 now-aware 比较
    exp_at = rt.expires_at if rt.expires_at.tzinfo else rt.expires_at.replace(tzinfo=UTC)
    if exp_at < now:
        raise UnauthorizedError("token_expired", code="token_expired")

    user_res = await db.execute(select(User).where(User.id == user_id))
    user = user_res.scalar_one_or_none()
    if user is None or not user.is_active:
        raise UnauthorizedError("user_inactive", code="user_inactive")

    # rotation：旧 refresh 标 revoked，签发新 pair
    rt.revoked_at = now
    tokens = await _issue_token_pair(db, user=user, settings=settings)
    await db.commit()
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """撤销给定 refresh token（必须属于当前 access user）。"""
    ip, ua = _client_meta(request)
    token_hash = hash_refresh_token(body.refresh_token)
    res = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.user_id == user.id,
        )
    )
    rt = res.scalar_one_or_none()
    if rt is not None and rt.revoked_at is None:
        rt.revoked_at = datetime.now(UTC)
    await write_audit(
        db,
        actor_user_id=user.id,
        action="user.logout",
        target_type="user",
        target_id=str(user.id),
        ip=ip,
        user_agent=ua,
    )
    await db.commit()


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse.model_validate(user, from_attributes=True)
