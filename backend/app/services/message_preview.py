"""把 favorites/notes 里 `target_type='message'` 的 target_id 解析成会话定位 + 内容预览。

收藏/笔记列表页要能"跳回原消息"并展示是哪条消息 → 需要 message → `session_id` + 内容预览。
target 已删 / target_id 非合法 message id → 该项不进返回 dict（调用方降级为 None）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message

_PREVIEW_LIMIT = 140


def make_preview(content: str) -> str:
    """折叠空白后截断，避免列表项里出现多余换行/缩进。"""
    return " ".join(content.split())[:_PREVIEW_LIMIT]


async def enrich_message_targets(
    db: AsyncSession, target_ids: list[str]
) -> dict[str, tuple[str, str]]:
    """message target_id(str) → (session_id str, content 预览)。非法/不存在的 id 不入表。"""
    parsed: dict[uuid.UUID, str] = {}
    for tid in target_ids:
        try:
            parsed[uuid.UUID(tid)] = tid
        except ValueError:
            continue
    if not parsed:
        return {}
    stmt = select(Message.id, Message.session_id, Message.content).where(
        Message.id.in_(list(parsed.keys()))
    )
    out: dict[str, tuple[str, str]] = {}
    for mid, sid, content in (await db.execute(stmt)).all():
        out[parsed[mid]] = (str(sid), make_preview(content or ""))
    return out
