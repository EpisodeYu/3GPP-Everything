"""`/api/v1/sessions/{sid}` checkpoint 路由（M4.8）。

5 个路由，分别包 `app.agent.checkpoint` 的 5 个纯函数：
- POST   /sessions/{sid}/runs/{rid}/pause   → pause_run
- POST   /sessions/{sid}/resume             → resume_run + 续跑 SSE
- GET    /sessions/{sid}/checkpoints        → list_checkpoints
- POST   /sessions/{sid}/fork               → fork_from + 新 DB session + 原会话标 archived_branch
- POST   /sessions/{sid}/rollback           → rollback + 删 PG 最后 N 条 message

文档锚 04-backend-api.md §M4.8 + 03-agent.md §11 / §12。

Resume 的 SSE 续跑沿用 `chat._build_sse_stream`（以 `initial_state=None` 进
`astream_events`）：避免与 send_message 实现漂移。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import and_, delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent import checkpoint as ckpt
from app.api.v1.chat import _build_sse_stream, _get_agent_graph, _get_cancel_registry
from app.core.auth import get_current_user
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.base import get_db
from app.db.models import Message, MessageCitation, User
from app.db.models import Session as DBSession
from app.schemas.checkpoint import (
    CheckpointListResponse,
    CheckpointOut,
    ForkBody,
    ForkResponse,
    PauseResponse,
    RollbackBody,
    RollbackResponse,
)
from app.schemas.sessions import SessionOut

router = APIRouter(prefix="/sessions", tags=["checkpoint"])


async def _load_owned_session(db: AsyncSession, sid: uuid.UUID, user_id: uuid.UUID) -> DBSession:
    res = await db.execute(
        select(DBSession).where(DBSession.id == sid, DBSession.user_id == user_id)
    )
    s = res.scalar_one_or_none()
    if s is None:
        raise NotFoundError("session_not_found", code="session_not_found")
    return s


async def _has_inflight_run(db: AsyncSession, sid: uuid.UUID) -> bool:
    """是否存在尚未收尾的 assistant 消息（content='' AND status='ok'）。

    M4.7 chat.py：assistant_msg 入口先 INSERT(content='', status='ok')；final/error
    后才 UPDATE。所以这一组合就是"跑中或卡住"的信号。
    """
    res = await db.execute(
        select(Message.id)
        .where(
            and_(
                Message.session_id == sid,
                Message.role == "assistant",
                Message.status == "ok",
                Message.content == "",
            )
        )
        .limit(1)
    )
    return res.scalar_one_or_none() is not None


# --- 1. pause -------------------------------------------------------------


@router.post(
    "/{sid}/runs/{rid}/pause",
    response_model=PauseResponse,
)
async def pause_run(
    sid: uuid.UUID,
    rid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PauseResponse:
    session = await _load_owned_session(db, sid, user.id)
    if session.status == "archived_branch":
        raise ConflictError("session_archived", code="session_archived")

    graph = _get_agent_graph(request)
    aupdate = getattr(graph, "aupdate_state", None)
    if aupdate is not None:
        # 与 cancel_run 对齐：thread 不存在等内部错误幂等吞下
        with contextlib.suppress(Exception):
            await ckpt.pause_run(graph, str(sid), rid)

    session.status = "paused"
    await db.commit()
    return PauseResponse(run_id=rid, session_id=sid, status="paused")


# --- 2. resume ------------------------------------------------------------


@router.post("/{sid}/resume")
async def resume_session(
    sid: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EventSourceResponse:
    session = await _load_owned_session(db, sid, user.id)
    if session.status == "archived_branch":
        raise ConflictError("session_archived", code="session_archived")
    if session.status != "paused":
        raise ConflictError("session_not_paused", code="session_not_paused")

    # 找出 paused 时遗留的 assistant stub（content='' AND status='ok'）
    res = await db.execute(
        select(Message)
        .where(
            and_(
                Message.session_id == sid,
                Message.role == "assistant",
                Message.status == "ok",
                Message.content == "",
            )
        )
        .order_by(desc(Message.created_at))
        .limit(1)
    )
    stub = res.scalar_one_or_none()
    if stub is None:
        # 没有遗留 stub：新建一条空 assistant msg 落地后续 final
        stub = Message(
            session_id=sid,
            role="assistant",
            content="",
            mode=session.mode_default,
            status="ok",
        )
        db.add(stub)
        await db.flush()
    run_id = stub.langgraph_run_id or uuid.uuid4().hex
    stub.langgraph_run_id = run_id

    # 清 paused flag → 准备续跑
    graph = _get_agent_graph(request)
    with contextlib.suppress(Exception):
        await ckpt.resume_run(graph, str(sid))

    session.status = "active"
    await db.commit()

    # SSE 续跑：initial_state=None（沿用 checkpoint 续跑语义）；同 chat 路径注册 cancel_event
    cancel_event = asyncio.Event()
    registry = _get_cancel_registry(request)
    registry[run_id] = cancel_event

    stream = _build_sse_stream(
        graph=graph,
        sid=sid,
        assistant_msg_id=stub.id,
        run_id=run_id,
        initial_state=None,
        db=db,
        cancel_event=cancel_event,
        cancel_registry=registry,
    )
    return EventSourceResponse(stream, ping=15, media_type="text/event-stream")


# --- 3. list checkpoints -------------------------------------------------


@router.get("/{sid}/checkpoints", response_model=CheckpointListResponse)
async def list_session_checkpoints(
    sid: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CheckpointListResponse:
    await _load_owned_session(db, sid, user.id)
    graph = _get_agent_graph(request)
    try:
        summaries = await ckpt.list_checkpoints(graph, str(sid))
    except Exception:
        summaries = []
    items = [
        CheckpointOut(
            checkpoint_id=s.checkpoint_id,
            parent_checkpoint_id=s.parent_checkpoint_id,
            created_at=s.created_at,
            next_nodes=list(s.next_nodes),
            last_node=s.last_node,
        )
        for s in summaries
    ]
    return CheckpointListResponse(items=items)


# --- 4. fork --------------------------------------------------------------


@router.post(
    "/{sid}/fork",
    status_code=status.HTTP_201_CREATED,
    response_model=ForkResponse,
)
async def fork_session(
    sid: uuid.UUID,
    body: ForkBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ForkResponse:
    session = await _load_owned_session(db, sid, user.id)
    if session.status == "archived_branch":
        raise ConflictError("session_archived", code="session_archived")

    new_session = DBSession(
        user_id=user.id,
        title=body.title or f"{session.title} (fork)",
        mode_default=session.mode_default,
        forked_from_session_id=sid,
        forked_from_checkpoint_id=body.checkpoint_id,
        status="active",
    )
    db.add(new_session)
    await db.flush()
    new_sid = new_session.id

    graph = _get_agent_graph(request)
    try:
        await ckpt.fork_from(
            graph,
            str(sid),
            body.checkpoint_id,
            str(new_sid),
            new_user_message=body.new_user_message,
        )
    except ValueError as exc:
        raise ValidationError(str(exc), code="checkpoint_not_found") from exc
    except RuntimeError as exc:
        raise ConflictError(str(exc), code="fork_unsupported") from exc

    session.status = "archived_branch"
    await db.commit()
    await db.refresh(new_session)
    return ForkResponse(new_session=SessionOut.model_validate(new_session, from_attributes=True))


# --- 5. rollback ----------------------------------------------------------


@router.post("/{sid}/rollback", response_model=RollbackResponse)
async def rollback_session(
    sid: uuid.UUID,
    body: RollbackBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RollbackResponse:
    session = await _load_owned_session(db, sid, user.id)
    if session.status == "archived_branch":
        raise ConflictError("session_archived", code="session_archived")
    if await _has_inflight_run(db, sid):
        raise ConflictError(
            "rollback_conflicts_with_active_run",
            code="rollback_conflicts_with_active_run",
        )

    # PG 侧：删最后 N 条 message（message_citations 通过 FK ondelete cascade
    # 自动清；这里显式 delete 以保兼容性）
    res = await db.execute(
        select(Message.id)
        .where(Message.session_id == sid)
        .order_by(desc(Message.created_at))
        .limit(body.last_n)
    )
    ids_to_delete = [row[0] for row in res.all()]
    deleted = 0
    if ids_to_delete:
        await db.execute(
            delete(MessageCitation).where(MessageCitation.message_id.in_(ids_to_delete))
        )
        d = await db.execute(delete(Message).where(Message.id.in_(ids_to_delete)))
        # AsyncResult 没有 type stub 暴露 rowcount；fallback 到 ids 长度
        deleted = getattr(d, "rowcount", None) or len(ids_to_delete)

    # LangGraph 侧：rollback last_n 个 checkpoint
    graph = _get_agent_graph(request)
    head = None
    try:
        head_summary = await ckpt.rollback(graph, str(sid), body.last_n)
        head = head_summary.checkpoint_id if head_summary else None
    except Exception:
        head = None

    # 状态归位：rollback 完肯定不是 paused
    session.status = "active"
    await db.commit()
    return RollbackResponse(deleted_messages=deleted, head_checkpoint_id=head)
