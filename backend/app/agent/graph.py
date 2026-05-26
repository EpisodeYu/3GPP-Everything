"""LangGraph 编译入口。

M4.2：`build_simple_graph(deps)` — simple fast path 五节点串成一条线。
M4.3：`build_graph(deps)` — 完整链路（仅 qa 模式；raw_lookup 已下线）：
  - query_class = tool → classify → tool_dispatch → generate → self_rag → END
        （`tool_dispatch` 按 `state.explicit_tools` 跑 glossary/toc/params/web_search；
        非 tool 类查询不走这条边，工具节点不会被触发——M4.4 验收第 3 条）
  - complexity = simple → classify → retrieve → rerank → generate → self_rag → END
  - complexity = complex → classify → rewrite → hyde → multi_query →
        retrieve → rerank → generate → self_rag → (retry → retrieve | END)

self_rag retry loop：
  - allow_retry=True；self_rag_node 在 verdict=retry 时把 retry_count += 1 +
    append missing_aspects 到 rewritten_queries
  - graph 条件边：verdict=retry AND retry_count < 2 → 回 retrieve；其它 → END
    （retry_count >= 2 即"强制收敛"，避免死循环）

依赖注入：节点签名是 `async def f(state, *, deps)`；graph 编译时用
`partial(f, deps=deps)` 把 deps 绑死在节点闭包里。
"""

from __future__ import annotations

from functools import partial
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .deps import AgentDeps
from .nodes import (
    classify_node,
    generate_node,
    hyde_node,
    multi_query_node,
    rerank_node,
    retrieve_node,
    rewrite_node,
    self_rag_node,
    tool_dispatch_node,
)
from .state import AgentState

_RETRY_CAP = 2


# ---------- M4.2 simple-only ----------


def build_simple_graph(deps: AgentDeps) -> CompiledStateGraph:
    """M4.2 simple fast path：classify → retrieve → rerank → generate → self_rag → END。

    self_rag 强制 `allow_retry=False`（不进 retry 循环；M4.3 build_graph 才允许）。
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


# ---------- M4.3 完整图 ----------


def build_graph(
    deps: AgentDeps,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph:
    """M4.3 完整链路：simple / complex 两路 + self-RAG retry（raw_lookup 已下线）。

    M4.5：`checkpointer` 可选；生产传入 `AsyncPostgresSaver`（thread_id=session_id），
    测试传 `InMemorySaver` 即可。不传 → 无持久化，单次 invoke 跑完不留 checkpoint。
    """
    builder: StateGraph[AgentState, Any, AgentState, AgentState] = StateGraph(AgentState)

    builder.add_node("classify", partial(classify_node, deps=deps))
    builder.add_node("rewrite", partial(rewrite_node, deps=deps))
    builder.add_node("hyde", partial(hyde_node, deps=deps))
    builder.add_node("multi_query", partial(multi_query_node, deps=deps))
    builder.add_node("tool_dispatch", partial(tool_dispatch_node, deps=deps))
    builder.add_node("retrieve", partial(retrieve_node, deps=deps))
    builder.add_node("rerank", partial(rerank_node, deps=deps))
    builder.add_node("generate", partial(generate_node, deps=deps))
    builder.add_node("self_rag", partial(self_rag_node, deps=deps, allow_retry=True))

    # START → classify（raw_lookup 已下线，不再按 mode 分流）
    builder.add_edge(START, "classify")

    # classify → 按 query_class / complexity 分流
    #   query_class=tool → tool_dispatch → generate（不走 retrieve；§3 状态图）
    #   complexity=complex → rewrite → hyde → multi_query → retrieve
    #   simple → retrieve
    builder.add_conditional_edges(
        "classify",
        _after_classify,
        {
            "tool": "tool_dispatch",
            "complex": "rewrite",
            "simple": "retrieve",
        },
    )

    # tool_dispatch → generate（工具结果由 generate_node 在 prompt 里消费）
    builder.add_edge("tool_dispatch", "generate")

    # complex 链路：rewrite → hyde → multi_query → retrieve
    builder.add_edge("rewrite", "hyde")
    builder.add_edge("hyde", "multi_query")
    builder.add_edge("multi_query", "retrieve")

    # retrieve → rerank → generate（raw_lookup 下线后 rerank 一律进生成）
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "generate")

    # generate → self_rag → 按 verdict / retry_count 决定回 retrieve 还是 END
    builder.add_edge("generate", "self_rag")
    builder.add_conditional_edges(
        "self_rag",
        _after_self_rag,
        {
            "retry": "retrieve",
            "end": END,
        },
    )

    return builder.compile(checkpointer=checkpointer)


# ---------- 路由判定（纯函数，便于单测） ----------


def _after_classify(state: AgentState) -> Literal["tool", "complex", "simple"]:
    if state.query_class == "tool":
        return "tool"
    return "complex" if state.complexity == "complex" else "simple"


def _after_self_rag(state: AgentState) -> Literal["retry", "end"]:
    if state.self_rag_verdict == "retry" and state.retry_count < _RETRY_CAP:
        return "retry"
    return "end"


# ---- lazy module-level singleton ----
#
# 生产代码 `from app.agent import tgpp_agent` 拿编译好的图。第一次取值时构造
# AgentDeps（连 LiteLLM / Qdrant / Redis），所以单测 / 没起依赖的环境下别 import
# 这个名字；用 `build_graph(stub_deps)` / `build_simple_graph(stub_deps)`。
_tgpp_agent_cache: CompiledStateGraph | None = None
_tgpp_agent_deps: AgentDeps | None = None


def _build_default() -> CompiledStateGraph:
    global _tgpp_agent_cache, _tgpp_agent_deps
    if _tgpp_agent_cache is None:
        _tgpp_agent_deps = AgentDeps.from_env()
        _tgpp_agent_cache = build_graph(_tgpp_agent_deps)
    return _tgpp_agent_cache


def __getattr__(name: str) -> Any:
    if name == "tgpp_agent":
        return _build_default()
    raise AttributeError(name)
