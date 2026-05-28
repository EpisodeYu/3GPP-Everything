"""`/api/v1/sessions` 会话 CRUD（M4.7）。

文档锚点：`docs/03-development/04-backend-api.md §M4.7` + `2026-05-17-m4.6-m4.9-decisions.md §Q14`。

权限：所有路由要求登录 user；只能看 / 改自己的 session（不区分 admin/user，admin
也走自己的 session 列表，不能透视别人的对话）。

口径：
- `status='archived_branch'`（M4.8 fork 后旧会话）：title patch 拒绝，但 DELETE 允许；
  POST /sessions/{sid}/messages（在 chat.py）也会拦下来。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.errors import ConflictError, NotFoundError
from app.db.base import get_db
from app.db.models import Session as DBSession
from app.db.models import User
from app.schemas.sessions import (
    SessionCreateBody,
    SessionListResponse,
    SessionOut,
    SessionPatchBody,
    SessionsBulkDeleteResponse,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


async def _load_owned(db: AsyncSession, sid: uuid.UUID, user_id: uuid.UUID) -> DBSession:
    res = await db.execute(
        select(DBSession).where(DBSession.id == sid, DBSession.user_id == user_id)
    )
    s = res.scalar_one_or_none()
    if s is None:
        raise NotFoundError("session_not_found", code="session_not_found")
    return s


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionListResponse:
    total = (
        await db.execute(
            select(func.count()).select_from(DBSession).where(DBSession.user_id == user.id)
        )
    ).scalar_one()
    offset = (page - 1) * page_size
    res = await db.execute(
        select(DBSession)
        .where(DBSession.user_id == user.id)
        .order_by(DBSession.updated_at.desc())
        .limit(page_size)
        .offset(offset)
    )
    items = [SessionOut.model_validate(s, from_attributes=True) for s in res.scalars().all()]
    return SessionListResponse(items=items, total=int(total))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SessionOut)
async def create_session(
    body: SessionCreateBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionOut:
    s = DBSession(
        user_id=user.id,
        title=body.title,
        mode_default=body.mode_default,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return SessionOut.model_validate(s, from_attributes=True)


@router.get("/{sid}", response_model=SessionOut)
async def get_session(
    sid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionOut:
    s = await _load_owned(db, sid, user.id)
    return SessionOut.model_validate(s, from_attributes=True)


@router.patch("/{sid}", response_model=SessionOut)
async def patch_session(
    sid: uuid.UUID,
    body: SessionPatchBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionOut:
    s = await _load_owned(db, sid, user.id)
    if body.title is not None:
        if s.status == "archived_branch":
            # Q14：archived_branch 拒绝 title 修改
            raise ConflictError("session_archived", code="session_archived")
        s.title = body.title
    if body.mode_default is not None:
        s.mode_default = body.mode_default
    await db.commit()
    await db.refresh(s)
    return SessionOut.model_validate(s, from_attributes=True)


@router.delete("", response_model=SessionsBulkDeleteResponse)
async def delete_all_sessions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionsBulkDeleteResponse:
    """清空当前用户的全部会话（含 archived_branch）。

    设计取舍：
    - 先 SELECT 出当前用户所有 session，再用 `session.delete(obj)` 逐个删；这条
      路径走 SQLAlchemy ORM 的 Python-level cascade（与 `DELETE /sessions/{sid}`
      一致），不依赖 DB 层 FK ON DELETE CASCADE。后者在 PG/MySQL 上也有效，但
      集成测试跑在 SQLite in-memory 上默认关 FK，靠 ORM cascade 才能保证 messages
      也被清掉。
    - N+1 query 对 "清空全部" 这种低频操作可接受（单用户上限通常 < 100 session）。
    - LangGraph checkpoint 表不在本路径清，与既有 `DELETE /sessions/{sid}` 行为
      一致（孤立 checkpoint 是已有的小遗留，不在本任务范围）。
    """
    res = await db.execute(select(DBSession).where(DBSession.user_id == user.id))
    sessions = list(res.scalars().all())
    for s in sessions:
        await db.delete(s)
    await db.commit()
    return SessionsBulkDeleteResponse(deleted=len(sessions))


@router.delete("/{sid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    sid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    s = await _load_owned(db, sid, user.id)
    await db.delete(s)
    await db.commit()
