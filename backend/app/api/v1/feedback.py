"""`/api/v1/messages/{mid}/feedback` 反馈（M4.9）。

每个 message 仅一条 feedback（unique constraint）；POST 同一 message 第二次 →
upsert（覆盖原 thumb / reason）。

权限：登录 user；只能反馈自己 session 下的 assistant message。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.errors import NotFoundError
from app.db.base import get_db
from app.db.models import Feedback, Message, User
from app.db.models import Session as DBSession
from app.schemas.feedback import FeedbackBody, FeedbackOut

router = APIRouter(prefix="/messages", tags=["feedback"])


@router.post(
    "/{mid}/feedback",
    status_code=status.HTTP_201_CREATED,
    response_model=FeedbackOut,
)
async def upsert_feedback(
    mid: uuid.UUID,
    body: FeedbackBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FeedbackOut:
    # 校验 message 存在且属于当前用户的 session
    msg_stmt = (
        select(Message, DBSession.user_id)
        .join(DBSession, DBSession.id == Message.session_id)
        .where(Message.id == mid)
    )
    row = (await db.execute(msg_stmt)).first()
    if row is None or row.user_id != user.id:
        raise NotFoundError("message_not_found", code="message_not_found")

    # upsert：同一 message 第二次提交直接覆盖
    fb_stmt = select(Feedback).where(Feedback.message_id == mid)
    fb = (await db.execute(fb_stmt)).scalar_one_or_none()
    if fb is None:
        fb = Feedback(
            user_id=user.id,
            message_id=mid,
            thumb=body.thumb,
            reason=body.reason,
        )
        db.add(fb)
    else:
        fb.thumb = body.thumb
        fb.reason = body.reason
        # 反馈所有权保持首次提交者；这里不改 user_id（如需 admin 强改另议）
    await db.commit()
    await db.refresh(fb)
    return FeedbackOut.model_validate(fb, from_attributes=True)
