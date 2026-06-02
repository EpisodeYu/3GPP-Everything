"""`/api/v1/sessions/{sid}` checkpoint 路由（M4.8）。

5 个路由，分别包 `app.agent.checkpoint` 的 5 个纯函数：
- POST   /sessions/{sid}/runs/{rid}/pause   → pause_run
- POST   /sessions/{sid}/resume             → resume_run + 续跑 SSE
- GET    /sessions/{sid}/checkpoints        → list_checkpoints
- POST   /sessions/{sid}/fork               → fork_from + 新 DB session
  （2026-06-01：原会话保持 active，不再 archive）
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
from sqlalchemy import and_, asc, delete, desc, select
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
            mode="qa",  # raw_lookup 已下线，新消息恒为 qa
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
    """从指定 checkpoint 起新建一个分支会话。

    **2026-06-01 行为变更**：fork 不再把原会话标记为 `archived_branch`。
    原会话保持原状态（通常仍是 `active`），用户可以在原会话继续对话；
    新会话独立创建且跳转过去，`forked_from_session_id` / `forked_from_checkpoint_id`
    保留追溯关系。

    **2026-06-02 行为变更**：fork 时把原会话的已完成历史消息（含 citations）复制
    到新会话的 PG `messages` 表 —— 否则分叉会话在前端（历史来自
    `GET /sessions/{sid}/messages`）看不到任何 fork 前的对话。LangGraph 侧
    `fork_from` 只拷 checkpoint state（供后续提问带上下文），不写 PG，故这里补齐。
    复制范围 = 原会话全部已完成消息（与 MVP「fork 一律用最近 checkpoint」一致），
    跳过 inflight / 空 assistant stub（`role=assistant && content=''`）。运行标识
    字段（run_id / checkpoint_id / trace_id / tokens）不复制 —— 历史消息是只读快照，
    新的 run 从用户在分叉会话里重新提问开始。

    `archived_branch` 这个 status 仍保留作向后兼容（M5 之前 fork 出的老会话已
    带此状态），相关只读 banner / 入口禁用逻辑维持不变；但本路由不会再产生新的
    `archived_branch` 会话。
    """
    session = await _load_owned_session(db, sid, user.id)
    if session.status == "archived_branch":
        # 老 archived 会话仍拒绝 fork（避免 fork-on-fork 长链） —— 仅向后兼容
        raise ConflictError("session_archived", code="session_archived")

    new_session = DBSession(
        user_id=user.id,
        title=body.title or f"{session.title} (fork)",
        mode_default="qa",  # raw_lookup 已下线，fork 出的会话恒为 qa
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

    await _copy_history_to_fork(db, sid, new_sid)

    # 原会话不再 archive：保持原状态让用户可继续对话
    await db.commit()
    await db.refresh(new_session)
    return ForkResponse(new_session=SessionOut.model_validate(new_session, from_attributes=True))


async def _copy_history_to_fork(db: AsyncSession, src_sid: uuid.UUID, new_sid: uuid.UUID) -> None:
    """把原会话 `src_sid` 的已完成历史消息（含 citations）复制到分叉会话 `new_sid`。

    保留原 `created_at` 维持时间顺序（messages list 按 `created_at asc` 返回）；
    跳过空 assistant stub（与前端 `_loadHistoryFromPg` 过滤一致）。citations 先按
    message 批量取出再分组复制，避免 async lazy-load relationship 触发 greenlet 错误。
    """
    src_msgs = list(
        (
            await db.execute(
                select(Message)
                .where(Message.session_id == src_sid)
                .order_by(asc(Message.created_at))
            )
        )
        .scalars()
        .all()
    )
    src_msgs = [m for m in src_msgs if not (m.role == "assistant" and not m.content)]
    if not src_msgs:
        return

    src_ids = [m.id for m in src_msgs]
    cites_by_msg: dict[uuid.UUID, list[MessageCitation]] = {}
    cite_rows = (
        (await db.execute(select(MessageCitation).where(MessageCitation.message_id.in_(src_ids))))
        .scalars()
        .all()
    )
    for c in cite_rows:
        cites_by_msg.setdefault(c.message_id, []).append(c)

    for m in src_msgs:
        copied = Message(
            session_id=new_sid,
            role=m.role,
            content=m.content,
            status=m.status,
            user_language=m.user_language,
            mode=m.mode,
            explicit_tools=list(m.explicit_tools or []),
            confidence=m.confidence,
            self_rag_verdict=m.self_rag_verdict,
            created_at=m.created_at,
        )
        db.add(copied)
        await db.flush()
        for c in cites_by_msg.get(m.id, []):
            db.add(
                MessageCitation(
                    message_id=copied.id,
                    chunk_meta_id=c.chunk_meta_id,
                    chunk_id=c.chunk_id,
                    rank=c.rank,
                    rerank_score=c.rerank_score,
                    spec_id=c.spec_id,
                    section_path=c.section_path,
                    char_offset_start=c.char_offset_start,
                    char_offset_end=c.char_offset_end,
                )
            )


# --- 5. rollback ----------------------------------------------------------


@router.post("/{sid}/rollback", response_model=RollbackResponse)
async def rollback_session(
    sid: uuid.UUID,
    body: RollbackBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RollbackResponse:
    """删除最后 N **轮**对话（message + checkpoint）。

    "一轮" = 1 条 user message + 它之后该会话内插入的所有 message（典型情形是
    user + assistant 一对，但中间也可能有节点级 stub / paused 残留 assistant）。

    实现（2026-06-01 fix#bug rollback）：
    1. 按 `(created_at desc, id desc)` 取最近 N 个 role='user' 消息；
    2. 第 N 个 user 的 `created_at` 作为 cutoff；
    3. 删除该 session 所有 `created_at >= cutoff` 的 message
       （cascade 到 message_citations）。

    旧实现按 "条数" 删（`order_by(created_at desc).limit(n)`），但 PG 同事务下
    user/assistant 的 `created_at = now()` 完全相同 → 排序不稳定，导致 last_n=1
    经常只删 user 把 assistant 留下。chat.py 入口侧已显式给 user 比 assistant
    早 10 微秒，再加上这里改成 "user 锚点 + cutoff" 双重保险。

    LangGraph 侧 `last_n` 仍为 checkpoint 数（一轮可能产生多个 checkpoint，但前
    端 UX 只关心 PG 视图，LangGraph rollback best-effort 即可）。
    """
    session = await _load_owned_session(db, sid, user.id)
    if session.status == "archived_branch":
        raise ConflictError("session_archived", code="session_archived")
    if await _has_inflight_run(db, sid):
        raise ConflictError(
            "rollback_conflicts_with_active_run",
            code="rollback_conflicts_with_active_run",
        )

    # 1. 取最近 N 个 user message 的 (id, created_at)
    res = await db.execute(
        select(Message.id, Message.created_at)
        .where(and_(Message.session_id == sid, Message.role == "user"))
        .order_by(desc(Message.created_at), desc(Message.id))
        .limit(body.last_n)
    )
    user_rows = res.all()

    deleted = 0
    if user_rows:
        cutoff_ts = user_rows[-1][1]
        # 2. 列出 cutoff 之后（含）的所有 message id
        ids_res = await db.execute(
            select(Message.id).where(
                and_(
                    Message.session_id == sid,
                    Message.created_at >= cutoff_ts,
                )
            )
        )
        ids_to_delete = [row[0] for row in ids_res.all()]
        if ids_to_delete:
            await db.execute(
                delete(MessageCitation).where(MessageCitation.message_id.in_(ids_to_delete))
            )
            d = await db.execute(delete(Message).where(Message.id.in_(ids_to_delete)))
            # AsyncResult 没有 type stub 暴露 rowcount；fallback 到 ids 长度
            deleted = getattr(d, "rowcount", None) or len(ids_to_delete)

    # LangGraph 侧：rollback last_n 个 checkpoint（best-effort）
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
