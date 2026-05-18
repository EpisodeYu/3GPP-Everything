"""`/api/v1/tools` 单独工具路由（M4.9）。

文档锚 04-backend-api.md §2 Tools 节。这些接口**不**走 Agent，给前端附加 UI 用：
- POST /tools/glossary/search → PG `glossary` normalized_term 精确匹配（限 1 个 term）
- POST /tools/toc             → `chunks_meta` 列指定 spec / section_prefix 子节

跟 `app.tools.glossary` / `app.tools.toc` 的 Agent 工具实现保持口径一致，但参数
来自请求 body 而非 AgentState；为避免循环引用且语义更清晰，这里独立实现 PG 查询。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.db.base import get_db
from app.db.models import ChunkMeta, Glossary, User
from app.schemas.tools import (
    GlossaryMatch,
    GlossarySearchBody,
    GlossarySearchResponse,
    TocBody,
    TocItem,
    TocResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


@router.post("/glossary/search", response_model=GlossarySearchResponse)
async def glossary_search(
    body: GlossarySearchBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> GlossarySearchResponse:
    normalized = body.term.strip().lower()
    stmt = select(Glossary).where(Glossary.normalized_term == normalized).limit(body.limit)
    rows = (await db.execute(stmt)).scalars().all()
    items = [
        GlossaryMatch(
            term=r.term,
            normalized_term=r.normalized_term,
            definition=r.definition,
            spec_id=r.spec_id,
            section_path=list(r.section_path or []),
        )
        for r in rows
    ]
    return GlossarySearchResponse(items=items)


@router.post("/toc", response_model=TocResponse)
async def toc(
    body: TocBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> TocResponse:
    clause_prefix = ".".join(p for p in body.section_prefix if p) or None
    stmt = select(ChunkMeta).where(ChunkMeta.spec_id == body.spec_id)
    if clause_prefix:
        stmt = stmt.where(ChunkMeta.clause.like(f"{clause_prefix}%"))
    stmt = stmt.order_by(ChunkMeta.document_order.asc()).limit(body.limit * 4)
    rows = (await db.execute(stmt)).scalars().all()

    seen: set[tuple[str, ...]] = set()
    items: list[TocItem] = []
    for r in rows:
        path_tuple = tuple(r.section_path or [])
        if path_tuple in seen:
            continue
        seen.add(path_tuple)
        items.append(
            TocItem(
                section_path=list(path_tuple),
                section_title=r.section_title or "",
                chunk_id=r.chunk_id,
                chunk_type=r.chunk_type,
            )
        )
        if len(items) >= body.limit:
            break
    return TocResponse(spec_id=body.spec_id, section_prefix=list(body.section_prefix), items=items)
