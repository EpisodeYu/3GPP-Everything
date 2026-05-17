"""Checkpoint 操作集（M4.5）。

口径 = `docs/03-development/03-agent.md §11 / §12`。后端 `04-backend-api.md` 在
HTTP / WebSocket 层包装这 5 个纯函数；这里不关心鉴权与 session 表（PG
`sessions.status = archived_branch` 由后端在 fork 之后单独写）。

5 个操作：
    list_checkpoints(graph, session_id)
    pause_run(graph, session_id, run_id)
    cancel_run(graph, session_id)
    resume_run(graph, session_id) -> RunnableConfig
    fork_from(graph, session_id, checkpoint_id, new_session_id, *,
              new_user_message=None) -> RunnableConfig
    rollback(graph, session_id, last_n)

设计：
- 全部以 `graph: CompiledStateGraph` 为入口；graph 必须已经 `compile(checkpointer=...)`。
  没接 checkpointer 时 fork / rollback 会抛 RuntimeError，其它退化为 best-effort
- thread_id == session_id；fork 用**新 thread_id** 实现（LangGraph 不支持同 thread 多分支）
- rollback 是"不可逆"：直接 `adelete_thread` 后用最后保留的 snapshot `aupdate_state`
  重新落一个 checkpoint，保证 graph.aget_state 直接返回保留点
- pause/cancel 只写状态字段（`paused`/`cancelled`），下一节点边界由
  `NodeInterrupt` 真正停下（§11）

注意：resume_run 不直接 `astream_events`；它只清 `paused` 并返回 config，由后端
SSE 路由自己 `astream_events(None, config=cfg)` 续跑（保持 SSE 流式与 API 层一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph


@dataclass
class CheckpointSummary:
    """`list_checkpoints` 返回项；与 §12 表格对齐。

    `next_nodes`：LangGraph snapshot `next` — 下一步要跑的节点元组（空元组 = 终止）。
    `last_node`：由 metadata.writes 推出来的"刚跑完的节点"；不可得时为 None。
    """

    checkpoint_id: str
    parent_checkpoint_id: str | None
    created_at: str
    next_nodes: tuple[str, ...] = ()
    last_node: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _thread_config(session_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
    cfg: dict[str, Any] = {"configurable": {"thread_id": session_id}}
    if checkpoint_id:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg  # type: ignore[return-value]


def _last_node(meta: dict[str, Any] | None) -> str | None:
    """LangGraph metadata.writes 是 {node_name: {field: value}}；取第一个键。"""
    if not meta:
        return None
    writes = meta.get("writes")
    if not writes:
        return None
    return next(iter(writes.keys()), None)


async def list_checkpoints(
    graph: CompiledStateGraph,
    session_id: str,
    *,
    limit: int | None = None,
) -> list[CheckpointSummary]:
    """按时间倒序返回所有 checkpoint summary。

    `limit=None` → 全部；`limit=N` → 仅最近 N 个。
    """
    cfg = _thread_config(session_id)
    out: list[CheckpointSummary] = []
    n = 0
    async for snap in graph.aget_state_history(cfg):
        c = snap.config.get("configurable", {})
        parent = None
        if snap.parent_config:
            parent = snap.parent_config.get("configurable", {}).get("checkpoint_id")
        out.append(
            CheckpointSummary(
                checkpoint_id=c.get("checkpoint_id", ""),
                parent_checkpoint_id=parent,
                created_at=snap.created_at or "",
                next_nodes=tuple(snap.next or ()),
                last_node=_last_node(dict(snap.metadata) if snap.metadata else None),
                metadata=dict(snap.metadata or {}),
            )
        )
        n += 1
        if limit is not None and n >= limit:
            break
    return out


async def pause_run(graph: CompiledStateGraph, session_id: str, run_id: str) -> RunnableConfig:
    """标记当前 run 为 paused；下一节点边界自然停下（§11 NodeInterrupt）。"""
    cfg = _thread_config(session_id)
    await graph.aupdate_state(cfg, {"paused": True, "run_id": run_id})
    return cfg


async def cancel_run(graph: CompiledStateGraph, session_id: str) -> RunnableConfig:
    """标记当前 run 为 cancelled；下一节点边界停下，会话语义视为终止。"""
    cfg = _thread_config(session_id)
    await graph.aupdate_state(cfg, {"cancelled": True})
    return cfg


async def resume_run(graph: CompiledStateGraph, session_id: str) -> RunnableConfig:
    """清 paused，返回 config；调用方拿到 cfg 后用 `astream_events(None, cfg)` 续跑。"""
    cfg = _thread_config(session_id)
    await graph.aupdate_state(cfg, {"paused": False})
    return cfg


async def fork_from(
    graph: CompiledStateGraph,
    session_id: str,
    checkpoint_id: str,
    new_session_id: str,
    *,
    new_user_message: str | None = None,
) -> RunnableConfig:
    """从 `session_id @ checkpoint_id` 起新分支到 `new_session_id`。

    原 session 在 PG `sessions` 表标 `archived_branch` 由后端做，这里只负责 LangGraph
    侧：拷贝 checkpoint 处的 state values → 写入 new_session_id 的初始 checkpoint，
    （可选）覆盖 user_input 触发后续节点。
    """
    if graph.checkpointer is None:
        raise RuntimeError("fork_from requires graph.checkpointer to be set")

    src_cfg = _thread_config(session_id, checkpoint_id)
    snap = await graph.aget_state(src_cfg)
    if snap is None or not snap.values:
        raise ValueError(f"checkpoint not found: session={session_id} ckpt={checkpoint_id}")

    values = dict(snap.values)
    if new_user_message is not None:
        values["user_input"] = new_user_message
        # 新分支重置 run 控制字段
        values["paused"] = False
        values["cancelled"] = False
        values["retry_count"] = 0

    dst_cfg = _thread_config(new_session_id)
    await graph.aupdate_state(dst_cfg, values)
    return dst_cfg


async def rollback(
    graph: CompiledStateGraph,
    session_id: str,
    last_n: int,
) -> CheckpointSummary | None:
    """删除最近 N 个 checkpoint，恢复到 N+1 轮末状态。

    实现：列出全部 history（newest first）→ `adelete_thread` → 用 history[N] 的
    values `aupdate_state` 重落一个 checkpoint。若 last_n 覆盖全部历史则直接清空
    thread 并返回 None（"全 rollback = 清会话"）。
    """
    if last_n < 0:
        raise ValueError("last_n must be >= 0")
    if last_n == 0:
        # nothing to rollback；返回当前 head
        head = await list_checkpoints(graph, session_id, limit=1)
        return head[0] if head else None

    saver = graph.checkpointer
    if not saver or isinstance(saver, bool):
        raise RuntimeError("rollback requires graph.checkpointer to be set")

    history = [s async for s in graph.aget_state_history(_thread_config(session_id))]
    if not history:
        return None
    if last_n >= len(history):
        await saver.adelete_thread(session_id)
        return None

    target = history[last_n]
    target_values = dict(target.values)
    await saver.adelete_thread(session_id)
    await graph.aupdate_state(_thread_config(session_id), target_values)

    head = await list_checkpoints(graph, session_id, limit=1)
    return head[0] if head else None
