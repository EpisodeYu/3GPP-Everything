"""compact_history 节点：图内会话历史压缩（2026-06-02 对齐 §6.1「由 deps 注入」）。

口径见 `docs/03-development/03-agent.md §6.1`。

把路由从 PG 重载的未压缩 prior 历史（`state.raw_history`，每条带 id）压缩成
`state.history`（`[summary?, 最近 N 条]`），供 contextualize / generate 节点消费。
compaction 经 `deps.llm`（summary LLM）+ `deps.redis`（summary 缓存）完成——即
「由 build_graph 的 deps 注入」，不再在 chat 路由里调 `compact_history`。

> 历史说明：旧实现把 compaction 放在 chat 路由，且依赖 `app.state.litellm_client`
> （生产从未接线 → 恒 None → summary 路径在生产实际从未触发）。移进图内用
> `deps.llm`（真实 client）后，长会话（> 8 prior）的 summary 才真正生效。

仅在 `state.raw_history` 非空时由 graph 条件入边触发（首轮直连 classify）。任何
LLM / 解析失败由 `compact_history()` 内部降级（保留最近 N 条），不阻塞主链路。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from langgraph.types import interrupt

from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.agent.utils.history_compactor import (
    HistoryMessage,
    compact_history,
    messages_to_role_dicts,
)

log = logging.getLogger(__name__)


def _to_history_messages(raw: list[dict[str, str]]) -> list[HistoryMessage]:
    """`state.raw_history` 的 `{id, role, content}` dict → `HistoryMessage`。

    id 解析失败（理论上不会，PG 永远是合法 uuid）兜底生成一个随机 uuid，只影响
    summary 缓存 key 命中、不影响正确性。
    """
    out: list[HistoryMessage] = []
    for m in raw:
        raw_id = m.get("id") or ""
        try:
            mid = uuid.UUID(raw_id)
        except (ValueError, AttributeError, TypeError):
            mid = uuid.uuid4()
        out.append(
            HistoryMessage(id=mid, role=m.get("role") or "user", content=m.get("content") or "")
        )
    return out


async def compact_history_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    if not state.raw_history:
        return {"history": []}

    history = _to_history_messages(state.raw_history)
    lc_messages = await compact_history(
        history,
        session_id=state.session_id or "",
        chat_client=deps.llm,
        redis=deps.redis,
    )
    return {"history": messages_to_role_dicts(lc_messages)}
