"""tool_dispatch 节点（M4.4）。

口径见 `docs/03-development/03-agent.md §3 + §4.9`。

行为：
- 读 `state.explicit_tools`，对每个已注册工具并发跑（asyncio.gather）
- 工具结果写入 `state.tool_results[tool_name]`
- 任意工具异常被 catch 成 warning，不阻塞其它工具
- 未列在 `explicit_tools` 的工具**不会**被调用（"显式触发" 守约，M4.4 验收第 3 条）
- 节点开头检测 cancelled / paused

返回：partial state update `{"tool_results": {<name>: <result>, ...}}`。

注意：`tool_results` 在 AgentState 里是 `dict[str, Any]`，**不**带 reducer，所以
返回的 dict 会**整体覆盖**当前 state.tool_results；这里特意把现有结果先 spread
进去再 merge 新结果，保留 graph 内多次进入 tool_dispatch 的累积（M4.5 retry 场景）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langgraph.types import interrupt

from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.tools import TOOL_REGISTRY

log = logging.getLogger(__name__)


async def tool_dispatch_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    selected: list[str] = []
    seen: set[str] = set()
    for name in state.explicit_tools or []:
        if name in TOOL_REGISTRY and name not in seen:
            selected.append(name)
            seen.add(name)

    if not selected:
        # 无显式工具 → 不调用任何工具（M4.4 验收第 3 条）
        return {"tool_results": dict(state.tool_results)}

    async def _safe_call(name: str) -> tuple[str, dict[str, Any]]:
        fn = TOOL_REGISTRY[name]
        try:
            res = await fn(state, deps)
        except Exception as exc:
            log.warning("tool_dispatch %r failed: %s", name, exc)
            return name, {"warning": f"{type(exc).__name__}: {exc}"}
        return name, res

    outcomes = await asyncio.gather(*(_safe_call(n) for n in selected))
    merged: dict[str, Any] = dict(state.tool_results)
    for name, res in outcomes:
        merged[name] = res
    return {"tool_results": merged}
