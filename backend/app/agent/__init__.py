"""LangGraph Agent（口径见 `docs/03-development/03-agent.md`）。

M4.2 已交付：simple fast path（classify / retrieve / rerank / generate /
self_rag grounding-only）+ AgentState + AgentDeps + 编译好的 `tgpp_agent`
（lazy 单例，第一次 import 才会构造真实依赖）。
"""

from typing import TYPE_CHECKING, Any

from . import checkpoint as checkpoint
from .deps import AgentDeps
from .graph import build_graph, build_simple_graph
from .langfuse_handler import (
    build_callback_handler,
    build_trace_metadata,
    init_langfuse,
)
from .state import AgentState, RetrievedChunk

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    tgpp_agent: CompiledStateGraph

__all__ = [
    "AgentDeps",
    "AgentState",
    "RetrievedChunk",
    "build_callback_handler",
    "build_graph",
    "build_simple_graph",
    "build_trace_metadata",
    "checkpoint",
    "init_langfuse",
    "tgpp_agent",
]


def __getattr__(name: str) -> Any:
    if name == "tgpp_agent":
        from . import graph as _graph

        return _graph._build_default()
    raise AttributeError(name)
