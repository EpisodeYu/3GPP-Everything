"""`/api/v1/docs` + `/api/v1/chunks` Reader 路由（M4.9）。

文档锚 04-backend-api.md §M4.9 / 路由总表 Reader 节。

数据源 = `chunks_meta`（M4.0 起 ingestion 写入；M8 crawler 接入后 `documents` 表
回填）。M4.9 只读 chunks_meta：
- list docs    → DISTINCT(spec_id, release, series) 聚合 chunk_count
- spec 详情    → DISTINCT(section_path)，按 document_order 排序
- 单 section   → spec_id + clause 前缀匹配，按 document_order 排序
- spec 内搜索  → `content ILIKE %q%`（最小可用；M7 之后若有需要再切 PG full-text）
- 单 chunk     → 按 chunk_id（不约束 provider；若有多 provider 取第一条）

权限：所有路由都要求登录 user；不区分 admin/user。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.db.base import get_db
from app.db.models import ChunkMeta, User
from app.schemas.docs import (
    ChunkOut,
    DocDetailResponse,
    DocListResponse,
    DocOut,
    SearchHit,
    SearchResponse,
    SectionDetailResponse,
    SectionNode,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["docs"])

_PREVIEW_CHARS = 240
_SEARCH_LIMIT = 50


@router.get("/docs", response_model=DocListResponse)
async def list_docs(
    release: str | None = Query(default=None, max_length=16),
    series: str | None = Query(default=None, max_length=8),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> DocListResponse:
    stmt = select(
        ChunkMeta.spec_id,
        ChunkMeta.release,
        ChunkMeta.series,
        ChunkMeta.title,
        func.count(ChunkMeta.id).label("chunk_count"),
    ).group_by(ChunkMeta.spec_id, ChunkMeta.release, ChunkMeta.series, ChunkMeta.title)
    if release:
        stmt = stmt.where(ChunkMeta.release == release)
    if series:
        stmt = stmt.where(ChunkMeta.series == series)
    stmt = stmt.order_by(ChunkMeta.spec_id.asc())
    rows = (await db.execute(stmt)).all()
    items = [
        DocOut(
            spec_id=r.spec_id,
            release=r.release,
            series=r.series,
            title=r.title or "",
            chunk_count=int(r.chunk_count or 0),
        )
        for r in rows
    ]
    return DocListResponse(items=items, total=len(items))


@router.get("/docs/{spec_id}", response_model=DocDetailResponse)
async def get_doc(
    spec_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> DocDetailResponse:
    head_stmt = (
        select(ChunkMeta.release, ChunkMeta.series, ChunkMeta.title)
        .where(ChunkMeta.spec_id == spec_id)
        .limit(1)
    )
    head = (await db.execute(head_stmt)).first()
    if head is None:
        raise NotFoundError("doc_not_found", code="doc_not_found")

    # 不在 SQL 层 GROUP BY (section_path)：PG 的 JSON 列没有等值算子（F-2）；
    # 拉全 spec 的 (section_path, section_title, document_order) 在 Python 里聚合。
    # 单 spec ~2-3k 行 × 3 个短字段，单次查询 < 100ms，足够 Reader 列章节树。
    sect_stmt = (
        select(ChunkMeta.section_path, ChunkMeta.section_title, ChunkMeta.document_order)
        .where(ChunkMeta.spec_id == spec_id)
        .order_by(ChunkMeta.document_order.asc())
    )
    rows = (await db.execute(sect_stmt)).all()

    # 按 (tuple(section_path), section_title) 聚合 → 保留首次出现的 document_order 作排序键
    grouped: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    for r in rows:
        sp_list = [str(x) for x in (r.section_path or [])]
        key = (tuple(sp_list), r.section_title or "")
        bucket = grouped.get(key)
        if bucket is None:
            grouped[key] = {
                "section_path": sp_list,
                "section_title": r.section_title or "",
                "ord": int(r.document_order or 0),
                "cnt": 1,
            }
        else:
            bucket["cnt"] += 1
    sections = [
        SectionNode(
            section_path=g["section_path"],
            section_title=g["section_title"],
            chunk_count=int(g["cnt"]),
        )
        for g in sorted(grouped.values(), key=lambda x: x["ord"])
    ]
    return DocDetailResponse(
        spec_id=spec_id,
        release=head.release or "",
        series=head.series or "",
        sections=sections,
    )


@router.get("/docs/{spec_id}/sections/{section_path:path}", response_model=SectionDetailResponse)
async def get_section(
    spec_id: str,
    section_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SectionDetailResponse:
    # section_path 既支持 "5.3.5"（点分），也容忍 "5/3/5"（URL 风格）
    normalized = section_path.replace("/", ".").strip(".")
    if not normalized:
        raise NotFoundError("section_not_found", code="section_not_found")

    stmt = (
        select(ChunkMeta)
        .where(ChunkMeta.spec_id == spec_id, ChunkMeta.clause.like(f"{normalized}%"))
        .order_by(ChunkMeta.document_order.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        raise NotFoundError("section_not_found", code="section_not_found")

    section_title = rows[0].section_title or ""
    content_map = await _fetch_content_map(request, [r.chunk_id for r in rows])
    chunks = [_chunk_to_out(c, content_override=content_map.get(c.chunk_id)) for c in rows]
    return SectionDetailResponse(
        spec_id=spec_id,
        section_path=normalized.split("."),
        section_title=section_title,
        chunks=chunks,
    )


@router.get("/docs/{spec_id}/search", response_model=SearchResponse)
async def search_in_doc(
    spec_id: str,
    q: str = Query(min_length=1, max_length=256),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SearchResponse:
    # M4.9 最小实现：ILIKE %q%；M7+ 视检索质量切 PG full-text 或 BM25
    pattern = f"%{q}%"
    stmt = (
        select(ChunkMeta)
        .where(ChunkMeta.spec_id == spec_id)
        .where(ChunkMeta.section_title.ilike(pattern) | ChunkMeta.clause.ilike(pattern))
        .order_by(ChunkMeta.document_order.asc())
        .limit(_SEARCH_LIMIT)
    )
    rows = (await db.execute(stmt)).scalars().all()
    items = [
        SearchHit(
            chunk_id=r.chunk_id,
            spec_id=r.spec_id,
            section_path=list(r.section_path or []),
            section_title=r.section_title or "",
            chunk_type=r.chunk_type,
            preview=_make_preview(r),
        )
        for r in rows
    ]
    return SearchResponse(spec_id=spec_id, query=q, items=items)


# 单 chunk 详情挂在另一个 prefix → 注册在 main.py 时不重复 prefix
chunks_router = APIRouter(tags=["docs"])


@chunks_router.get("/chunks/{chunk_id}", response_model=ChunkOut)
async def get_chunk(
    chunk_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ChunkOut:
    stmt = select(ChunkMeta).where(ChunkMeta.chunk_id == chunk_id).limit(1)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("chunk_not_found", code="chunk_not_found")
    content_map = await _fetch_content_map(request, [chunk_id])
    return _chunk_to_out(row, content_override=content_map.get(chunk_id))


def _chunk_to_out(c: ChunkMeta, *, content_override: str | None = None) -> ChunkOut:
    raw: dict[str, Any] = c.raw_extra if isinstance(c.raw_extra, dict) else {}
    # F-4：content 优先来自 Qdrant payload（ingestion 实际只把 content 写进 Qdrant，
    # PG chunks_meta 不存 content）。Qdrant 不可达时退回 raw_extra（满足集成测 SQLite 路径）。
    if content_override is not None:
        content = content_override
    else:
        content = str(raw.get("content") or raw.get("text") or "")
    return ChunkOut(
        chunk_id=c.chunk_id,
        spec_id=c.spec_id,
        section_path=list(c.section_path or []),
        section_title=c.section_title or "",
        chunk_type=c.chunk_type,
        content=content,
        char_offset_start=c.char_offset_start,
        char_offset_end=c.char_offset_end,
        raw_extra=raw,
    )


def _make_preview(c: ChunkMeta) -> str:
    raw: dict[str, Any] = c.raw_extra if isinstance(c.raw_extra, dict) else {}
    text = str(raw.get("content") or raw.get("text") or "") or (c.section_title or "")
    return text[:_PREVIEW_CHARS]


async def _fetch_content_map(request: Request, chunk_ids: list[str]) -> dict[str, str]:
    """批量从 Qdrant 拉 content；失败/无 client 时返回空 dict（调用方退回 raw_extra）。

    生产路径：`app.state.qdrant_client` 在首次访问时 lazy 构造（与 main 不强耦合，
    避免冷启动 Qdrant 不可达 → API 起不来）。集成测路径：app.state 不预置 client
    → 直接返回 {}，让 `_chunk_to_out` 走 raw_extra fallback。

    Qdrant point id = chunk_id（uuid5，ingestion 端 `_ensure_qdrant_point_id` 直接透传）。
    """
    if not chunk_ids:
        return {}
    client, collection = await _get_qdrant(request)
    if client is None or not collection:
        return {}
    try:
        points = await client.retrieve(
            collection_name=collection,
            ids=list(chunk_ids),
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:
        log.debug("qdrant retrieve failed for %d ids: %s", len(chunk_ids), exc)
        return {}
    out: dict[str, str] = {}
    for p in points:
        payload = dict(getattr(p, "payload", {}) or {})
        cid = str(payload.get("chunk_id") or p.id)
        content = payload.get("content")
        if content:
            out[cid] = str(content)
    return out


async def _get_qdrant(request: Request) -> tuple[Any | None, str]:
    """惰性单例：第一次访问时建 AsyncQdrantClient，挂到 app.state。

    返回 (client, collection_name)；client=None 表示当前环境没接 Qdrant（测试/没配 URL）。
    """
    state = request.app.state
    client = getattr(state, "qdrant_client", None)
    collection: str = getattr(state, "qdrant_collection", "") or ""
    if client is not None:
        return client, collection

    # `qdrant_client_disabled = True` 让测试显式关掉（也可以不设，默认无 URL 就不连）
    if getattr(state, "qdrant_client_disabled", False):
        return None, ""

    s = get_settings()
    if not s.QDRANT_URL:
        return None, ""
    try:
        from qdrant_client import AsyncQdrantClient

        api_key = s.QDRANT_API_KEY.get_secret_value() or None
        state.qdrant_client = AsyncQdrantClient(url=s.QDRANT_URL, api_key=api_key)
        state.qdrant_collection = s.qdrant_collection
        return state.qdrant_client, state.qdrant_collection
    except Exception as exc:
        log.warning("qdrant client init failed; chunk content will fallback: %s", exc)
        state.qdrant_client_disabled = True
        return None, ""
