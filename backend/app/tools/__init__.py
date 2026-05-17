"""Agent 工具集（M4.4）。

口径见 `docs/03-development/03-agent.md §4.9`。每个工具是
    async def tool(state, *, deps) -> dict[str, Any]
返回结构化 dict，由 `agent/nodes/tool_dispatch.py` 写入 `state.tool_results[<name>]`。

`TOOL_REGISTRY` 是工具名 → 调用函数的注册表；`tool_dispatch` 节点按
`state.explicit_tools` 选择运行哪些工具（"显式触发" 守约）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.deps import AgentDeps
from app.agent.state import AgentState

from .glossary import glossary_tool
from .params import params_tool
from .toc import toc_tool
from .web_search import web_search_tool

ToolFn = Callable[[AgentState, AgentDeps], Awaitable[dict[str, Any]]]


async def _glossary(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    return await glossary_tool(state, deps=deps)


async def _toc(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    return await toc_tool(state, deps=deps)


async def _params(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    return await params_tool(state, deps=deps)


async def _web_search(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    return await web_search_tool(state, deps=deps)


TOOL_REGISTRY: dict[str, ToolFn] = {
    "glossary": _glossary,
    "toc": _toc,
    "params": _params,
    "web_search": _web_search,
}

__all__ = [
    "TOOL_REGISTRY",
    "ToolFn",
    "glossary_tool",
    "params_tool",
    "toc_tool",
    "web_search_tool",
]
