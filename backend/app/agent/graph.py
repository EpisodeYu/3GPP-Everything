"""LangGraph 编译入口。

M4.2 只导出 `build_simple_graph(deps)` + `tgpp_agent`（lazy 编译，避免 import 时
连真实依赖）。complex / raw_lookup / tool_dispatch 分支在 M4.3 / M4.4 增量加。

依赖注入：
- 节点签名是 `async def f(state, *, deps)`；graph 编译时用 `partial(f, deps=deps)`
  把 deps 绑死在节点闭包里。LangGraph 不需要知道 deps 的存在
- 测试可直接构造 stub `AgentDeps` 喂 `build_simple_graph(stub_deps)`，无需 mock
  全局
"""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .deps import AgentDeps
from .nodes import (
    classify_node,
    generate_node,
    rerank_node,
    retrieve_node,
    self_rag_node,
)
from .state import AgentState


def build_simple_graph(deps: AgentDeps) -> CompiledStateGraph:
    """M4.2 simple fast path：classify → retrieve → rerank → generate → self_rag → END。

    self_rag 强制 `allow_retry=False`（不进 retry 循环；M4.3 起 complex 分支才允许）。
    """
    builder: StateGraph[AgentState, Any, AgentState, AgentState] = StateGraph(AgentState)

    builder.add_node("classify", partial(classify_node, deps=deps))
    builder.add_node("retrieve", partial(retrieve_node, deps=deps))
    builder.add_node("rerank", partial(rerank_node, deps=deps))
    builder.add_node("generate", partial(generate_node, deps=deps))
    builder.add_node(
        "self_rag",
        partial(self_rag_node, deps=deps, allow_retry=False),
    )

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "generate")
    builder.add_edge("generate", "self_rag")
    builder.add_edge("self_rag", END)

    return builder.compile()


# ---- lazy module-level singleton ----
#
# 生产代码 `from app.agent import tgpp_agent` 拿编译好的图。第一次取值时构造
# AgentDeps（连 LiteLLM / Qdrant / Redis），所以单测 / 没起依赖的环境下别 import
# 这个名字；用 `build_simple_graph(stub_deps)` 即可。
_tgpp_agent_cache: CompiledStateGraph | None = None
_tgpp_agent_deps: AgentDeps | None = None


def _build_default() -> CompiledStateGraph:
    global _tgpp_agent_cache, _tgpp_agent_deps
    if _tgpp_agent_cache is None:
        _tgpp_agent_deps = AgentDeps.from_env()
        _tgpp_agent_cache = build_simple_graph(_tgpp_agent_deps)
    return _tgpp_agent_cache


def __getattr__(name: str) -> Any:
    if name == "tgpp_agent":
        return _build_default()
    raise AttributeError(name)
