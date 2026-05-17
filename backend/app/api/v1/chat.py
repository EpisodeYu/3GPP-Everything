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
from app.core.errors import ConflictError, NotFoundError
from app.core.ratelimit import rate_limit
from app.db.base import get_db
from app.db.models import Message, MessageCitation, User
from app.db.models import Session as DBSession
from app.schemas.chat import SendMessageBody

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
    """先取测试注入的 fake，再 fallback prod 单例。"""
    override = getattr(request.app.state, "agent_graph", None)
    if override is not None:
        return override
    from app.agent import tgpp_agent  # lazy：测试环境不连依赖也能 import 本模块

    return tgpp_agent


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
) -> EventSourceResponse:
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
    mode_eff = body.mode or session.mode_default

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

    async def stream() -> AsyncIterator[dict[str, str]]:
        yield _sse(
            "run_start",
            {
                "run_id": run_id,
                "session_id": str(sid),
                "message_id": str(assistant_msg_id),
            },
        )

        final_state: dict[str, Any] | None = None
        error_msg: str | None = None
        was_cancelled = False
        node_start_ts: dict[str, float] = {}

        try:
            async for evt in graph.astream_events(
                initial_state,
                config={"configurable": {"thread_id": str(sid)}},
                version="v2",
            ):
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
                    chunk = data.get("chunk")
                    delta = ""
                    if chunk is not None:
                        # langchain AIMessageChunk has .content
                        delta = getattr(chunk, "content", "") or ""
                    if delta:
                        yield _sse("token", {"delta": delta})
                elif kind == "on_custom_event" and name in ("chunks_hit", "chunks_rerank"):
                    yield _sse(name, data)
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as exc:
            log.exception("chat stream agent failure: run_id=%s", run_id)
            error_msg = str(exc) or exc.__class__.__name__

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

        yield _sse("end", {})

    return EventSourceResponse(
        stream(),
        ping=15,  # Q8：每 15s `: ping` 注释行
        media_type="text/event-stream",
    )


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

    graph = _get_agent_graph(request)
    # aupdate_state 让正在跑的图在下个节点 raise NodeInterrupt
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
