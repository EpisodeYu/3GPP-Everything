"""`/api/v1/sessions/{sid}/messages` SSE 流式 chat + `/runs/{rid}` 取消（M4.7）。

文档锚点：`docs/03-development/04-backend-api.md §4` + `03-agent.md §7 / §11` +
`2026-05-17-m4.6-m4.9-decisions.md §一 Q6-Q10`。

SSE event 列表（10 类）：
    run_start / node_start / node_end / chunks_hit / chunks_rerank /
    token / final / end / cancelled / error

Q9 落盘策略：assistant message 在路由入口先插入一行（content=""，status="ok"），
final event 之后一次性 `UPDATE messages SET content=...`；中断 → status='cancelled'
/'failed'，content 保持空。

测试注入：路由优先读 `request.app.state.agent_graph`（fake 图），fallback 走 prod
单例 `tgpp_agent`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import asc, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent.state import AgentState
from app.agent.utils.history_compactor import RECENT_N, HistoryMessage, compact_history
from app.core.auth import get_current_user
from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.ratelimit import rate_limit
from app.db.base import get_db
from app.db.models import Message, MessageCitation, User
from app.db.models import Session as DBSession
from app.schemas.chat import SendMessageBody
from app.services.usage import set_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["chat"])

# Agent 节点白名单：astream_events 的 on_chain_start/end 也会触发非节点（如
# graph root、reducer、并发分支等），我们只把节点事件透传给前端。
_NODE_NAMES: set[str] = {
    "classify",
    "rewrite",
    "hyde",
    "multi_query",
    "tool_dispatch",
    "retrieve",
    "rerank",
    "generate",
    "self_rag",
}


def _get_agent_graph(request: Request) -> Any:
    """先取测试注入 / lifespan 构造的 graph，再 fallback prod lazy 单例。"""
    override = getattr(request.app.state, "agent_graph", None)
    if override is not None:
        return override
    from app.agent import tgpp_agent  # lazy：测试环境不连依赖也能 import 本模块

    return tgpp_agent


def _get_title_client(request: Request) -> Any:
    """首轮自动标题用的 LLM client。

    解析顺序：
    1. `app.state.title_client`（测试注入 / 显式覆盖）
    2. `app.state._agent_deps.llm`（lifespan 成功初始化时设置）
    3. lazy `tgpp_agent` 单例对应的 `_tgpp_agent_deps.llm`：覆盖 lifespan 因
       上游不可达 swallow 异常的场景（chat 路由会 fallback 到 lazy agent，但
       deps 之前没有暴露给 title 路径，导致自动标题永远跳过）
    都缺 → None，caller 跳过自动标题。
    """
    override = getattr(request.app.state, "title_client", None)
    if override is not None:
        return override
    deps = getattr(request.app.state, "_agent_deps", None)
    llm = getattr(deps, "llm", None)
    if llm is not None:
        return llm
    # lazy fallback：触发一次 `tgpp_agent` 构造（与 _get_agent_graph 同源），
    # 然后从 graph 模块的 cache 里读 deps.llm。任何 ImportError / 构造失败都
    # swallow，自动标题降级为 no-op。
    try:
        from app.agent import graph as _graph

        _graph._build_default()  # 触发 lazy 构造（已构造过则直接返回缓存）
        lazy_deps = _graph._tgpp_agent_deps
        return getattr(lazy_deps, "llm", None)
    except Exception as exc:
        log.debug("title_client lazy fallback failed: %s", exc)
        return None


def _get_cancel_registry(request: Request) -> dict[str, asyncio.Event]:
    """`app.state.in_flight_cancels`：run_id → asyncio.Event。

    DELETE /sessions/{sid}/runs/{rid} 设事件；SSE 流在 race loop 里检测 → 立即
    `task.cancel()` 正在 await 的 astream_events 迭代器，让 LLM streaming 中段就停。
    需要在请求进入前由 lifespan 或 conftest 初始化；缺失时按需 lazy 建（多 worker
    部署下不同 worker 持有各自 registry，cancel 命中率取决于 run 是否在同 worker —
    M4 单进程 dev 不受影响）。
    """
    state = request.app.state
    reg: dict[str, asyncio.Event] | None = getattr(state, "in_flight_cancels", None)
    if reg is None:
        reg = {}
        state.in_flight_cancels = reg
    return reg


def _build_initial_state(
    *,
    body: SendMessageBody,
    user_language: str,
    history: list[Any],
    session_default_mode: str,
    run_id: str,
) -> AgentState:
    return AgentState(
        user_input=body.content,
        user_language="zh" if user_language == "zh" else "en",
        mode=body.mode or session_default_mode,  # type: ignore[arg-type]
        explicit_tools=body.explicit_tools,
        messages=history,
        run_id=run_id,
    )


def _sse(event: str, data: Any) -> dict[str, str]:
    """sse-starlette EventSourceResponse 接受 {event, data} dict。"""
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


def _summary_for_node_end(node: str, output: Any) -> dict[str, Any]:
    """node_end summary：把每个节点对前端有用的几个字段挑出来，控制 payload 体积。"""
    if not isinstance(output, dict):
        return {}
    keep_keys = {
        "classify": ("query_class", "complexity"),
        "rewrite": ("rewritten_queries",),
        "hyde": ("hyde_doc",),
        "multi_query": ("rewritten_queries",),
        "retrieve": ("candidates",),
        "rerank": ("reranked",),
        "self_rag": ("self_rag_verdict", "retry_count"),
    }.get(node, ())
    out: dict[str, Any] = {}
    for k in keep_keys:
        v = output.get(k)
        if v is None:
            continue
        # list of pydantic models → count；其他 scalar 透传
        if isinstance(v, list):
            out[f"{k}_count"] = len(v)
        else:
            out[k] = v
    return out


async def _load_history(
    db: AsyncSession, session_id: uuid.UUID, exclude_id: uuid.UUID | None = None
) -> list[HistoryMessage]:
    res = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(asc(Message.created_at))
    )
    rows = res.scalars().all()
    return [
        HistoryMessage(id=m.id, role=m.role, content=m.content)
        for m in rows
        if exclude_id is None or m.id != exclude_id
    ]


@router.post(
    "/{sid}/messages",
    dependencies=[Depends(rate_limit("chat"))],
)
async def send_message(
    sid: uuid.UUID,
    body: SendMessageBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> EventSourceResponse:
    # M7.4：把 user.id 装进 ContextVar，下游 LiteLLMClient / web_search_tool 的
    # usage hook 自动读到；ContextVar 在 asyncio Task 内部传递，不污染其他请求。
    # 不在 finally 里 reset：本 task 在 SSE 流结束时自然结束，ContextVar 也随之释放。
    set_current_user(user.id)

    # 1. 会话权属 + 状态校验
    res = await db.execute(
        select(DBSession).where(DBSession.id == sid, DBSession.user_id == user.id)
    )
    session = res.scalar_one_or_none()
    if session is None:
        raise NotFoundError("session_not_found", code="session_not_found")
    if session.status == "archived_branch":
        # Q14：archived 会话不能继续发消息
        raise ConflictError("session_archived", code="session_archived")

    run_id = uuid.uuid4().hex
    # raw_lookup 已下线，mode 恒为 qa（body.mode/session.mode_default 仅可能是 qa 或历史脏值）
    mode_eff = "qa"

    # 2. 落 user message + assistant stub
    user_msg = Message(
        session_id=sid,
        role="user",
        content=body.content,
        mode=mode_eff,
        explicit_tools=body.explicit_tools,
        status="ok",
    )
    db.add(user_msg)
    assistant_msg = Message(
        session_id=sid,
        role="assistant",
        content="",
        mode=mode_eff,
        explicit_tools=body.explicit_tools,
        status="ok",
        langgraph_run_id=run_id,
    )
    db.add(assistant_msg)
    await db.flush()
    assistant_msg_id = assistant_msg.id
    await db.commit()

    # 3. 拼历史（含本轮 user message；compact_history 决定是否 summary）
    raw_history = await _load_history(db, sid, exclude_id=assistant_msg_id)
    redis = getattr(request.app.state, "redis", None)
    chat_client = getattr(request.app.state, "litellm_client", None)

    lc_history: list[BaseMessage]
    if chat_client is not None:
        lc_history = await compact_history(
            raw_history,
            session_id=sid,
            chat_client=chat_client,
            redis=redis,
        )
    else:
        # 没有 LLM client（早期 / 测试场景）：仅拿最近 N 条 user/assistant 原文
        lc_history = []
        for m in raw_history[-RECENT_N:]:
            if m.role == "user":
                lc_history.append(HumanMessage(content=m.content))
            elif m.role == "assistant":
                lc_history.append(AIMessage(content=m.content))

    initial_state = _build_initial_state(
        body=body,
        user_language="en",
        history=lc_history,
        session_default_mode=session.mode_default,
        run_id=run_id,
    )

    graph = _get_agent_graph(request)

    # F-1：在 app.state 注册 cancel_event；DELETE /runs/{rid} 设事件 → race loop 命中
    cancel_event = asyncio.Event()
    registry = _get_cancel_registry(request)
    registry[run_id] = cancel_event

    # 首轮自动标题：仅当会话标题仍为空（新建 / 自动标题失败过）才在本轮成功后生成。
    autotitle_question = body.content if not (session.title or "").strip() else None

    stream = _build_sse_stream(
        graph=graph,
        sid=sid,
        assistant_msg_id=assistant_msg_id,
        run_id=run_id,
        initial_state=initial_state,
        db=db,
        cancel_event=cancel_event,
        cancel_registry=registry,
        autotitle_question=autotitle_question,
        title_client=_get_title_client(request),
        title_model=settings.LLM_LIGHT_MODEL,
    )
    return EventSourceResponse(
        stream,
        ping=15,  # Q8：每 15s `: ping` 注释行
        media_type="text/event-stream",
    )


def _build_sse_stream(
    *,
    graph: Any,
    sid: uuid.UUID,
    assistant_msg_id: uuid.UUID,
    run_id: str,
    initial_state: Any,
    db: AsyncSession,
    cancel_event: asyncio.Event | None = None,
    cancel_registry: dict[str, asyncio.Event] | None = None,
    autotitle_question: str | None = None,
    title_client: Any = None,
    title_model: str | None = None,
) -> AsyncIterator[dict[str, str]] | Any:
    """构造 SSE 事件 generator；send_message 与 checkpoint resume 共用。

    `initial_state`：send_message 路径传完整 AgentState；resume 路径传 None
    （LangGraph 续跑语义：用 thread checkpointer 里的最后 state 继续）。

    `cancel_event`：可选，外部 DELETE /runs/{rid} set 后 race loop 会立刻 cancel
    正在 await 的 astream_events 迭代器，让 LLM streaming 中途也能停。None →
    退化为原 best-effort 路径（仅靠 aupdate_state 的 cancelled flag）。

    `autotitle_question` + `title_client`：仅 send_message 首轮（空标题会话）传；
    本路径**与 agent 完全并发**用 LIGHT 模型起标题，回写 session.title 并在
    `title` 事件 yield；title 一旦就绪就立即 emit（可能出现在 `node_start` /
    `token` 之间，前端 sidebar 立即生效）；agent 跑完时若 title 还没好，给 2s
    短超时兜底，超时后被取消（不阻塞 `end`）。resume 路径不传 → 不触发。
    """

    async def stream() -> AsyncIterator[dict[str, str]]:
        yield _sse(
            "run_start",
            {
                "run_id": run_id,
                "session_id": str(sid),
                "message_id": str(assistant_msg_id),
            },
        )

        # autotitle 与 agent 完全并发：用户问题已经落 PG（见上方 user_msg flush），
        # 不依赖 agent 答案，提前启动让标题尽快出现在 sidebar / chat header。
        title_task: asyncio.Task[str | None] | None = None
        if autotitle_question and title_client is not None:
            title_task = asyncio.create_task(
                _run_autotitle_llm(
                    question=autotitle_question,
                    chat_client=title_client,
                    model=title_model or "",
                )
            )
        title_emitted = False

        async def _try_emit_title() -> dict[str, str] | None:
            """title_task 已 done → 写库 + 返回要 yield 的 SSE event；否则 None。"""
            nonlocal title_emitted
            if title_emitted or title_task is None or not title_task.done():
                return None
            title_emitted = True
            try:
                new_title = title_task.result()
            except asyncio.CancelledError:
                return None
            except Exception as exc:
                log.warning("autotitle llm failed: run_id=%s err=%s", run_id, exc)
                return None
            if not new_title:
                return None
            try:
                await db.execute(
                    update(DBSession).where(DBSession.id == sid).values(title=new_title)
                )
                await db.commit()
            except Exception as exc:
                log.warning("autotitle db persist failed: run_id=%s err=%s", run_id, exc)
                return None
            return _sse("title", {"session_id": str(sid), "title": new_title})

        final_state: dict[str, Any] | None = None
        error_msg: str | None = None
        was_cancelled = False
        node_start_ts: dict[str, float] = {}

        events_iter = graph.astream_events(
            initial_state,
            config={"configurable": {"thread_id": str(sid)}},
            version="v2",
        )
        try:
            async for evt in _iter_with_cancel(events_iter, cancel_event):
                if evt is _CANCEL_SENTINEL:
                    was_cancelled = True
                    break
                kind = evt.get("event")
                name = evt.get("name") or ""
                data = evt.get("data") or {}

                if kind == "on_chain_start" and name in _NODE_NAMES:
                    loop = asyncio.get_event_loop()
                    node_start_ts[name] = loop.time()
                    yield _sse("node_start", {"node": name})
                elif kind == "on_chain_end" and name in _NODE_NAMES:
                    loop = asyncio.get_event_loop()
                    dur_ms = int((loop.time() - node_start_ts.get(name, loop.time())) * 1000)
                    yield _sse(
                        "node_end",
                        {
                            "node": name,
                            "duration_ms": dur_ms,
                            "summary": _summary_for_node_end(name, data.get("output")),
                        },
                    )
                elif kind == "on_chain_end" and name == "LangGraph":
                    # graph 顶层结束：拿到完整 final state
                    output = data.get("output")
                    if isinstance(output, dict):
                        final_state = output
                    else:
                        # AgentState pydantic instance
                        try:
                            final_state = output.model_dump()  # type: ignore[union-attr]
                        except Exception:
                            final_state = None
                elif kind == "on_chat_model_stream":
                    # 保留：测试 / 未来若引入 LangChain ChatModel 时仍可走此路径。
                    # 生产路径由 generate 节点的 `on_custom_event` name="token" 提供（见下）。
                    chunk = data.get("chunk")
                    delta = ""
                    if chunk is not None:
                        # langchain AIMessageChunk has .content
                        delta = getattr(chunk, "content", "") or ""
                    if delta:
                        yield _sse("token", {"delta": delta})
                elif kind == "on_custom_event" and name == "token":
                    # generate 节点用 LiteLLMClient.chat_stream() 真流式拉 token，
                    # 通过 adispatch_custom_event("token", {"delta":...}) 推过来。
                    delta = ""
                    if isinstance(data, dict):
                        # adispatch_custom_event 把 payload 直接放进 data；
                        # 防御性也支持嵌套 data["data"]（不同 LangGraph 版本差异）
                        d_payload: Any = data.get("data") if "data" in data else data
                        if isinstance(d_payload, dict):
                            delta = str(d_payload.get("delta") or "")
                    if delta:
                        yield _sse("token", {"delta": delta})
                elif kind == "on_custom_event" and name in ("chunks_hit", "chunks_rerank"):
                    yield _sse(name, data)

                # 每次 LangGraph 事件后 poll 一次 title task；done 就立刻 emit。
                # 即便上面 if/elif 都没 yield（如非节点的 on_chain_start），也保持低
                # 延迟检查。LIGHT 模型一般几百 ms 就能返回，标题可能在第一个 token 之
                # 前就到。
                title_evt = await _try_emit_title()
                if title_evt is not None:
                    yield title_evt
        except asyncio.CancelledError:
            # ASGI 客户端断开 / 服务关闭 — 没人接收事件了，直接退出，不再写 DB
            if title_task is not None and not title_task.done():
                title_task.cancel()
            with contextlib.suppress(Exception):
                await events_iter.aclose()
            if cancel_registry is not None:
                cancel_registry.pop(run_id, None)
            raise
        except Exception as exc:
            log.exception("chat stream agent failure: run_id=%s", run_id)
            error_msg = str(exc) or exc.__class__.__name__
        finally:
            # 确保 LangGraph 流被关闭，避免后台 LLM 调用继续燃烧 token
            with contextlib.suppress(Exception):
                await events_iter.aclose()

        # 检测 graph 通过 cancelled flag 优雅退出（aupdate_state cancelled=True）
        if (
            final_state is not None
            and final_state.get("cancelled")
            and not final_state.get("final_answer")
        ):
            was_cancelled = True
            final_state = None

        # 4. 持久化 + 收尾事件
        if was_cancelled:
            await db.execute(
                update(Message).where(Message.id == assistant_msg_id).values(status="cancelled")
            )
            await db.commit()
            yield _sse("cancelled", {"reason": "user_cancelled"})
        elif error_msg is not None:
            await db.execute(
                update(Message).where(Message.id == assistant_msg_id).values(status="failed")
            )
            await db.commit()
            yield _sse("error", {"code": "agent_failed", "message": error_msg})
        elif final_state is not None:
            answer = final_state.get("final_answer") or ""
            citations = final_state.get("citations") or []
            confidence = final_state.get("confidence") or 0.0
            await db.execute(
                update(Message)
                .where(Message.id == assistant_msg_id)
                .values(
                    content=answer,
                    confidence=float(confidence),
                    self_rag_verdict=final_state.get("self_rag_verdict"),
                    langgraph_checkpoint_id=str(final_state.get("trace_id") or "") or None,
                    langfuse_trace_id=str(final_state.get("trace_id") or "") or None,
                )
            )
            for rank, cit in enumerate(citations):
                db.add(
                    MessageCitation(
                        message_id=assistant_msg_id,
                        chunk_id=str(cit.get("chunk_id") or ""),
                        rank=rank,
                        rerank_score=cit.get("rerank_score"),
                        spec_id=str(cit.get("spec_id") or ""),
                        section_path=str(cit.get("section_path") or ""),
                    )
                )
            await db.commit()
            yield _sse(
                "final",
                {
                    "message_id": str(assistant_msg_id),
                    "answer": answer,
                    "citations": citations,
                    "confidence": confidence,
                },
            )
        else:
            # 没拿到 final_state 也没 error：保守标 failed
            await db.execute(
                update(Message).where(Message.id == assistant_msg_id).values(status="failed")
            )
            await db.commit()
            yield _sse(
                "error",
                {"code": "no_final_state", "message": "graph_did_not_produce_final"},
            )

        # autotitle 兜底：agent 流跑完但 title 还没好（罕见，LIGHT 模型一般几百 ms）
        # → 给 2s 短窗口等一下；超时就放弃，cancel 后台 task，不阻塞 end。
        if title_task is not None and not title_emitted:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(title_task), timeout=2.0)
            title_evt = await _try_emit_title()
            if title_evt is not None:
                yield title_evt
        if title_task is not None and not title_task.done():
            title_task.cancel()

        yield _sse("end", {})

        # 收尾：清 cancel registry。CancelledError 路径已在 except 里清过
        if cancel_registry is not None:
            cancel_registry.pop(run_id, None)

    return stream()


async def _run_autotitle_llm(
    *, question: str, chat_client: Any, model: str
) -> str | None:
    """autotitle LLM 包装：只跑 LLM 不动 DB；DB 写由主 task 串行做，避免并发占同一
    AsyncSession（SQLAlchemy AsyncSession 不是 task-safe）。
    """
    from app.services.session_title import generate_session_title

    return await generate_session_title(
        question=question,
        chat_client=chat_client,
        model=model,
    )


_CANCEL_SENTINEL: Any = object()


async def _iter_with_cancel(
    events_iter: Any,
    cancel_event: asyncio.Event | None,
) -> AsyncIterator[Any]:
    """以 asyncio.race 形式包 LangGraph astream_events 迭代器。

    cancel_event 为 None → 直接转发；cancel_event set → 取消正在 await 的
    `__anext__`（让 LiteLLM streaming 调用收到 CancelledError），yield 一次
    `_CANCEL_SENTINEL` 通知 stream 退出循环。
    """
    if cancel_event is None:
        async for evt in events_iter:
            yield evt
        return

    cancel_task: asyncio.Task[Any] = asyncio.ensure_future(cancel_event.wait())
    try:
        while True:
            evt_task: asyncio.Task[Any] = asyncio.ensure_future(events_iter.__anext__())
            done, _pending = await asyncio.wait(
                {evt_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done:
                evt_task.cancel()
                with contextlib.suppress(BaseException):
                    await evt_task
                yield _CANCEL_SENTINEL
                return
            try:
                evt = evt_task.result()
            except StopAsyncIteration:
                return
            yield evt
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
            with contextlib.suppress(BaseException):
                await cancel_task


@router.delete(
    "/{sid}/runs/{rid}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_run(
    sid: uuid.UUID,
    rid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    # 会话权属校验
    res = await db.execute(
        select(DBSession.id).where(DBSession.id == sid, DBSession.user_id == user.id)
    )
    if res.scalar_one_or_none() is None:
        raise NotFoundError("session_not_found", code="session_not_found")

    # F-1：优先走 cancel_event race（mid-LLM 也能停）；同时把 cancelled flag
    # 写入 checkpoint 供 resume / debug 可见。两条通道幂等共存，DELETE 返回 204
    # 表示"请求已收到"，不依赖 run 是否实际在跑（前端只关心后续 SSE 是否出
    # cancelled / final 事件）。
    registry = _get_cancel_registry(request)
    event = registry.get(rid)
    if event is not None:
        event.set()

    graph = _get_agent_graph(request)
    aupdate = getattr(graph, "aupdate_state", None)
    if aupdate is not None:
        try:
            await aupdate(
                config={"configurable": {"thread_id": str(sid)}},
                values={"cancelled": True, "run_id": rid},
            )
        except Exception as exc:
            # 不向用户暴露细节（可能 thread_id 已不存在）；幂等返回 204
            log.warning("cancel_run aupdate_state failed: %s", exc)
