"""Message → MessageOut 序列化（含 citations）。

messages.py（用户读自己会话）与 admin.py（admin 读任意会话）共用同一套口径，
避免两处 MessageOut 构造漂移。
"""

from __future__ import annotations

import uuid

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, MessageCitation
from app.schemas.messages import MessageCitationOut, MessageOut


async def citations_for(
    db: AsyncSession, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[MessageCitationOut]]:
    if not message_ids:
        return {}
    res = await db.execute(
        select(MessageCitation)
        .where(MessageCitation.message_id.in_(message_ids))
        .order_by(MessageCitation.message_id, asc(MessageCitation.rank))
    )
    out: dict[uuid.UUID, list[MessageCitationOut]] = {}
    for c in res.scalars().all():
        out.setdefault(c.message_id, []).append(
            MessageCitationOut(
                chunk_id=c.chunk_id,
                rank=c.rank,
                spec_id=c.spec_id,
                section_path=c.section_path,
                rerank_score=c.rerank_score,
                char_offset_start=c.char_offset_start,
                char_offset_end=c.char_offset_end,
            )
        )
    return out


def message_to_out(m: Message, citations: list[MessageCitationOut]) -> MessageOut:
    return MessageOut(
        id=m.id,
        session_id=m.session_id,
        role=m.role,  # type: ignore[arg-type]
        content=m.content,
        status=m.status,  # type: ignore[arg-type]
        mode=m.mode,
        explicit_tools=list(m.explicit_tools or []),
        confidence=m.confidence,
        self_rag_verdict=m.self_rag_verdict,
        langgraph_run_id=m.langgraph_run_id,
        created_at=m.created_at,
        citations=citations,
    )
