"""`/api/v1/users` admin 路由：list / create / patch（启停 / 改角色 / 重置密码）。

文档锚点：`docs/03-development/04-backend-api.md §2 / §5`。

权限：所有路由要求 `role=admin`（`require_role("admin")`）。

审计点：
- user.create
- user.update（is_active 变更 / role 变更 / password 重置）
- user.disable / user.enable（is_active 切换的特例，便于 grep）
- user.role_change

停用 / 改角色 / 重置密码后，撤销该用户所有未失效的 refresh_token。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.auth import hash_password, require_role
from app.core.errors import ConflictError, NotFoundError
from app.db.base import get_db
from app.db.models import RefreshToken, User
from app.schemas.user import (
    UserCreateBody,
    UserListResponse,
    UserOut,
    UserPatchBody,
)

router = APIRouter(prefix="/users", tags=["users"])


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua


async def _revoke_all_refresh_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


@router.get("", response_model=UserListResponse)
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> UserListResponse:
    total = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    offset = (page - 1) * page_size
    res = await db.execute(
        select(User).order_by(User.created_at.desc()).limit(page_size).offset(offset)
    )
    items = [UserOut.model_validate(u, from_attributes=True) for u in res.scalars().all()]
    return UserListResponse(items=items, total=int(total))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=UserOut)
async def create_user(
    body: UserCreateBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("admin")),
) -> UserOut:
    ip, ua = _client_meta(request)
    # 唯一性预检
    dup = (
        await db.execute(select(User.id).where(User.username == body.username))
    ).scalar_one_or_none()
    if dup is not None:
        raise ConflictError("username_taken", code="username_taken")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password.get_secret_value()),
        role=body.role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    await write_audit(
        db,
        actor_user_id=admin.id,
        action="user.create",
        target_type="user",
        target_id=str(user.id),
        ip=ip,
        user_agent=ua,
        extra={"role": body.role, "username": body.username},
    )
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user, from_attributes=True)


@router.patch("/{uid}", response_model=UserOut)
async def patch_user(
    uid: uuid.UUID,
    body: UserPatchBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("admin")),
) -> UserOut:
    ip, ua = _client_meta(request)
    res = await db.execute(select(User).where(User.id == uid))
    user = res.scalar_one_or_none()
    if user is None:
        raise NotFoundError("user_not_found", code="user_not_found")

    changes: dict[str, object] = {}
    audit_actions: list[tuple[str, dict[str, object]]] = []
    revoke_tokens = False

    if body.is_active is not None and body.is_active != user.is_active:
        changes["is_active"] = body.is_active
        user.is_active = body.is_active
        action = "user.enable" if body.is_active else "user.disable"
        audit_actions.append((action, {"is_active": body.is_active}))
        if not body.is_active:
            revoke_tokens = True

    if body.role is not None and body.role != user.role:
        changes["role"] = body.role
        old_role = user.role
        user.role = body.role
        audit_actions.append(("user.role_change", {"from": old_role, "to": body.role}))
        revoke_tokens = True

    if body.password is not None:
        user.password_hash = hash_password(body.password.get_secret_value())
        audit_actions.append(("user.password_reset", {}))
        changes["password"] = "reset"
        revoke_tokens = True

    if revoke_tokens:
        await _revoke_all_refresh_tokens(db, user.id)

    if not audit_actions:
        # 没有任何变化，直接回当前态（不写 audit）
        await db.commit()
        await db.refresh(user)
        return UserOut.model_validate(user, from_attributes=True)

    for action, extra in audit_actions:
        await write_audit(
            db,
            actor_user_id=admin.id,
            action=action,
            target_type="user",
            target_id=str(user.id),
            ip=ip,
            user_agent=ua,
            extra=extra,
        )

    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user, from_attributes=True)
