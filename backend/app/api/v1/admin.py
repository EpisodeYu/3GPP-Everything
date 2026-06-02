"""`/api/v1/admin/*` 管理路由（M4.10）。

文档锚点：`docs/03-development/04-backend-api.md §2 Admin / §9.1 / §M4.10`。

权限：所有路由要求 `role=admin`。

M4.10 范围：
- GET  /admin/stats             — 索引数 / chunk 数 / 任务桶 / 7 天 API 用量
- GET  /admin/tasks              — 异步任务列表（按 created_at desc，支持 status filter）
- GET  /admin/tasks/{tid}        — 单个任务状态
- POST /admin/index/rebuild      — 触发索引重建（asyncio.create_task 简化版）

不实现（M4 主动推迟，到 M8 上线前补）：
- POST /admin/crawl              — FTP 爬虫 trigger（M4 走 CLI）
- POST /admin/upload-doc         — 单文档 docling 兜底链路

Audit：
- admin.index_rebuild 触发 → audit_logs 写一行（Q5 决策范围内）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.auth import require_role
from app.core.errors import NotFoundError
from app.core.ratelimit import rate_limit
from app.db.base import get_db
from app.db.models import (
    ApiUsage,
    ChunkMeta,
    Document,
    Feedback,
    Message,
    Task,
    User,
)
from app.db.models import Session as DBSession
from app.schemas.admin import (
    AdminFeedbackItem,
    AdminFeedbackListResponse,
    ApiUsage7dOut,
    FeedbackStatsOut,
    IndexRebuildBody,
    StatsOut,
    TaskListResponse,
    TaskOut,
)
from app.services.message_preview import make_preview
from app.services.task_runner import TaskRunner, default_task_runner, schedule_task

router = APIRouter(prefix="/admin", tags=["admin"])


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua


def _get_task_runner(request: Request) -> TaskRunner:
    """测试可注入 `app.state.task_runner` 桩；缺省走 subprocess runner。"""
    override = getattr(request.app.state, "task_runner", None)
    return override if override is not None else default_task_runner


# === stats ===


@router.get(
    "/stats",
    response_model=StatsOut,
    summary="Admin: 索引/chunk/任务/API 用量统计",
)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> StatsOut:
    """返回索引文档数、chunk 数、用户/会话/消息计数、任务按 status 分桶、最近 7 天 API 用量聚合。"""
    documents = int((await db.execute(select(func.count()).select_from(Document))).scalar_one())
    chunks = int((await db.execute(select(func.count()).select_from(ChunkMeta))).scalar_one())
    users = int((await db.execute(select(func.count()).select_from(User))).scalar_one())
    sessions = int((await db.execute(select(func.count()).select_from(DBSession))).scalar_one())
    messages = int((await db.execute(select(func.count()).select_from(Message))).scalar_one())

    rows = (await db.execute(select(Task.status, func.count()).group_by(Task.status))).all()
    tasks_by_status: dict[str, int] = {str(s): int(c) for s, c in rows}

    today = datetime.now(UTC).date()
    cutoff = today - timedelta(days=7)
    usage_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(ApiUsage.llm_input_tokens), 0),
                func.coalesce(func.sum(ApiUsage.llm_output_tokens), 0),
                func.coalesce(func.sum(ApiUsage.embedding_tokens), 0),
                func.coalesce(func.sum(ApiUsage.rerank_calls), 0),
                func.coalesce(func.sum(ApiUsage.web_search_calls), 0),
                func.coalesce(func.sum(ApiUsage.total_cost_usd), 0.0),
            ).where(ApiUsage.day >= cutoff)
        )
    ).one()
    api_usage_7d = ApiUsage7dOut(
        llm_input_tokens=int(usage_row[0] or 0),
        llm_output_tokens=int(usage_row[1] or 0),
        embedding_tokens=int(usage_row[2] or 0),
        rerank_calls=int(usage_row[3] or 0),
        web_search_calls=int(usage_row[4] or 0),
        total_cost_usd=float(usage_row[5] or 0.0),
    )

    return StatsOut(
        documents=documents,
        chunks=chunks,
        users=users,
        sessions=sessions,
        messages=messages,
        tasks=tasks_by_status,
        api_usage_7d=api_usage_7d,
    )


# === feedback ===


@router.get(
    "/feedback",
    response_model=AdminFeedbackListResponse,
    summary="Admin: 用户点赞/点踩反馈（聚合计数 + 明细列表）",
)
async def list_feedback(
    thumb: int | None = Query(default=None, description="过滤 1=赞 / -1=踩；缺省全部"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> AdminFeedbackListResponse:
    """聚合计数取全量（不受 thumb/分页影响）；列表 join 消息预览 + 反馈者 + 会话定位。"""
    agg = (await db.execute(select(Feedback.thumb, func.count()).group_by(Feedback.thumb))).all()
    counts = {int(t): int(c) for t, c in agg}
    up, down = counts.get(1, 0), counts.get(-1, 0)
    stats = FeedbackStatsOut(up=up, down=down, total=up + down)

    base = (
        select(Feedback, Message.content, Message.session_id, User.username)
        .join(Message, Message.id == Feedback.message_id)
        .join(User, User.id == Feedback.user_id)
    )
    count_base = select(func.count()).select_from(Feedback)
    if thumb in (1, -1):
        base = base.where(Feedback.thumb == thumb)
        count_base = count_base.where(Feedback.thumb == thumb)

    total = int((await db.execute(count_base)).scalar_one())
    offset = (page - 1) * page_size
    rows = (
        await db.execute(base.order_by(desc(Feedback.created_at)).limit(page_size).offset(offset))
    ).all()
    items = [
        AdminFeedbackItem(
            id=fb.id,
            message_id=fb.message_id,
            session_id=session_id,
            thumb=fb.thumb,
            reason=fb.reason,
            username=username,
            message_preview=make_preview(content or ""),
            created_at=fb.created_at,
        )
        for fb, content, session_id, username in rows
    ]
    return AdminFeedbackListResponse(stats=stats, items=items, total=total)


# === tasks ===


def _task_to_out(t: Task) -> TaskOut:
    return TaskOut(
        id=t.id,
        kind=t.kind,  # type: ignore[arg-type]
        payload=dict(t.payload or {}),
        status=t.status,  # type: ignore[arg-type]
        progress=int(t.progress),
        log_tail=t.log_tail or "",
        started_at=t.started_at,
        finished_at=t.finished_at,
        created_by=t.created_by,
        created_at=t.created_at,
    )


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    summary="Admin: 列出异步任务",
)
async def list_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> TaskListResponse:
    """按 created_at 倒序列出 tasks，支持 status filter（queued/running/done/failed）。"""
    base = select(Task)
    count_base = select(func.count()).select_from(Task)
    if status_filter:
        base = base.where(Task.status == status_filter)
        count_base = count_base.where(Task.status == status_filter)

    total = int((await db.execute(count_base)).scalar_one())
    offset = (page - 1) * page_size
    rows = (
        (await db.execute(base.order_by(desc(Task.created_at)).limit(page_size).offset(offset)))
        .scalars()
        .all()
    )
    return TaskListResponse(items=[_task_to_out(t) for t in rows], total=total)


@router.get(
    "/tasks/{tid}",
    response_model=TaskOut,
    summary="Admin: 取单个异步任务详情",
)
async def get_task(
    tid: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> TaskOut:
    """按 ID 取任务详情；不存在 → 404。"""
    res = await db.execute(select(Task).where(Task.id == tid))
    t = res.scalar_one_or_none()
    if t is None:
        raise NotFoundError("task_not_found", code="task_not_found")
    return _task_to_out(t)


# === index/rebuild ===


@router.post(
    "/index/rebuild",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskOut,
    dependencies=[Depends(rate_limit("admin_crawl"))],
    summary="Admin: 触发索引重建",
)
async def trigger_index_rebuild(
    body: IndexRebuildBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("admin")),
) -> TaskOut:
    """写 Task 行 + asyncio.create_task 包 ingestion CLI；同步返回 queued 状态的 TaskOut。

    M4 简化版（详见 04-backend-api.md §9.1）：runner 在同进程 event loop 里跑；
    M8 上线前换 Redis Streams worker。
    """
    payload: dict[str, Any] = body.model_dump(exclude_none=False)
    task = Task(
        kind="index_rebuild",
        payload=payload,
        status="queued",
        progress=0,
        log_tail="",
        created_by=admin.id,
    )
    db.add(task)
    await db.flush()

    ip, ua = _client_meta(request)
    await write_audit(
        db,
        actor_user_id=admin.id,
        action="admin.index_rebuild",
        target_type="task",
        target_id=str(task.id),
        ip=ip,
        user_agent=ua,
        extra={"payload": payload},
    )
    await db.commit()
    await db.refresh(task)

    runner = _get_task_runner(request)
    schedule_task(runner, task_id=task.id, kind="index_rebuild", payload=payload)

    return _task_to_out(task)
