"""`/api/v1/sessions/{sid}/messages` 消息列表 + 详情（F-5 / F-6）。

文档锚 04-backend-api.md §2 路由总表。M4.7 只挂了 send_message（POST）与 cancel_run；
list/get 路由 M4 范围内漏注册，2026-05-19 端到端人审 暴露后补。

权限：同 chat.py / sessions.py — 只能看自己会话的消息（admin 也走自己的会话，不透视别人）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import asc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.errors import NotFoundError
from app.db.base import get_db
from app.db.models import Message, User
from app.db.models import Session as DBSession
from app.schemas.messages import MessageListResponse, MessageOut
from app.services.message_serialize import citations_for, message_to_out

router = APIRouter(prefix="/sessions", tags=["messages"])


async def _assert_owned(db: AsyncSession, sid: uuid.UUID, user_id: uuid.UUID) -> None:
    res = await db.execute(
        select(DBSession.id).where(DBSession.id == sid, DBSession.user_id == user_id)
    )
    if res.scalar_one_or_none() is None:
        raise NotFoundError("session_not_found", code="session_not_found")


@router.get("/{sid}/messages", response_model=MessageListResponse)
async def list_messages(
    sid: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageListResponse:
    await _assert_owned(db, sid, user.id)

    total = (
        await db.execute(select(func.count()).select_from(Message).where(Message.session_id == sid))
    ).scalar_one()
    offset = (page - 1) * page_size
    res = await db.execute(
        select(Message)
        .where(Message.session_id == sid)
        .order_by(asc(Message.created_at))
        .limit(page_size)
        .offset(offset)
    )
    rows = list(res.scalars().all())
    cits = await citations_for(db, [m.id for m in rows])
    items = [message_to_out(m, cits.get(m.id, [])) for m in rows]
    return MessageListResponse(items=items, total=int(total))


@router.get("/{sid}/messages/{mid}", response_model=MessageOut)
async def get_message(
    sid: uuid.UUID,
    mid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    await _assert_owned(db, sid, user.id)
    res = await db.execute(select(Message).where(Message.id == mid, Message.session_id == sid))
    m = res.scalar_one_or_none()
    if m is None:
        raise NotFoundError("message_not_found", code="message_not_found")
    cits = await citations_for(db, [m.id])
    return message_to_out(m, cits.get(m.id, []))
