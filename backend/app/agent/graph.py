"""LangGraph 编译入口。

M4.2：`build_simple_graph(deps)` — simple fast path 五节点串成一条线。
M4.3：`build_graph(deps)` — 完整链路（仅 qa 模式；raw_lookup 已下线）：
  - query_class = tool → classify → tool_dispatch → generate → self_rag → END
        （`tool_dispatch` 按 `state.explicit_tools` 跑 glossary/toc/params/web_search；
        非 tool 类查询不走这条边，工具节点不会被触发——M4.4 验收第 3 条）
  - complexity = simple → classify → retrieve → rerank → expand → generate → self_rag → END
  - complexity = complex（或 query_class = definition）→ classify → rewrite → hyde →
        multi_query → retrieve → rerank → expand → generate → self_rag → (retry → retrieve | END)
  （expand = small2big 回扩 parent section，Issue #3；开关关/无 db/无 parent 时透传）
        （定义题虽常被判 simple，但最需要精准命中唯一定义条款，强制走扩展链提升召回；
        见 `_after_classify`）

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
    compact_history_node,
    contextualize_node,
    expand_node,
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
    """M4.2 simple fast path：classify → retrieve → rerank → expand → generate → self_rag → END。

    self_rag 强制 `allow_retry=False`（不进 retry 循环；M4.3 build_graph 才允许）。
    """
    builder: StateGraph[AgentState, Any, AgentState, AgentState] = StateGraph(AgentState)

    builder.add_node("classify", partial(classify_node, deps=deps))
    builder.add_node("retrieve", partial(retrieve_node, deps=deps))
    builder.add_node("rerank", partial(rerank_node, deps=deps))
    builder.add_node("expand", partial(expand_node, deps=deps))
    builder.add_node("generate", partial(generate_node, deps=deps))
    builder.add_node(
        "self_rag",
        partial(self_rag_node, deps=deps, allow_retry=False),
    )

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "rerank")
    # small2big（Issue #3）：rerank 后经 expand 把命中小块回扩为整段 section 再进 generate。
    # expand 在开关关 / 无 db / 无 parent 时透传（返回 {}），节点仍执行但零改状态。
    builder.add_edge("rerank", "expand")
    builder.add_edge("expand", "generate")
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

    2026-06-02 接通真多轮：START 经条件入边 `_entry_router`，`state.raw_history` 非空
    （多轮追问）时先走 `compact_history`（图内压缩历史，§6.1 deps 注入）→ `contextualize`
    （指代消解）→ classify；首轮直连 classify。

    M4.5：`checkpointer` 可选；生产传入 `AsyncPostgresSaver`（thread_id=session_id），
    测试传 `InMemorySaver` 即可。不传 → 无持久化，单次 invoke 跑完不留 checkpoint。
    """
    builder: StateGraph[AgentState, Any, AgentState, AgentState] = StateGraph(AgentState)

    builder.add_node("compact_history", partial(compact_history_node, deps=deps))
    builder.add_node("contextualize", partial(contextualize_node, deps=deps))
    builder.add_node("classify", partial(classify_node, deps=deps))
    builder.add_node("rewrite", partial(rewrite_node, deps=deps))
    builder.add_node("hyde", partial(hyde_node, deps=deps))
    builder.add_node("multi_query", partial(multi_query_node, deps=deps))
    builder.add_node("tool_dispatch", partial(tool_dispatch_node, deps=deps))
    builder.add_node("retrieve", partial(retrieve_node, deps=deps))
    builder.add_node("rerank", partial(rerank_node, deps=deps))
    builder.add_node("expand", partial(expand_node, deps=deps))
    builder.add_node("generate", partial(generate_node, deps=deps))
    builder.add_node("self_rag", partial(self_rag_node, deps=deps, allow_retry=True))

    # START → 仅多轮（有 raw_history）才先走 compact_history（压缩历史，§6.1 deps 注入）
    # → contextualize（指代消解）→ classify；首轮（无 raw_history）直连 classify。
    # 条件入边而非无条件接入：首轮零额外节点/LLM 调用，也让既有单/集成测的精确节点
    # 序列断言不被首轮场景破坏。
    builder.add_conditional_edges(
        START,
        _entry_router,
        {
            "compact_history": "compact_history",
            "classify": "classify",
        },
    )
    builder.add_edge("compact_history", "contextualize")
    builder.add_edge("contextualize", "classify")

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

    # retrieve → rerank → expand → generate（raw_lookup 下线后 rerank 一律进生成）
    # small2big（Issue #3）：expand 在 rerank 与 generate 之间回扩 parent section；
    # 开关关 / 无 db / 无 parent 时透传。tool 路径不经 expand（tool_dispatch → generate）。
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "expand")
    builder.add_edge("expand", "generate")

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


def _entry_router(state: AgentState) -> Literal["compact_history", "classify"]:
    # 有 prior 历史（多轮追问）→ 先压缩历史（图内 compact）+ 指代消解；首轮无历史
    # 直连 classify（省 compact/contextualize 两个节点 + 其 LLM 调用）。
    return "compact_history" if state.raw_history else "classify"


def _after_classify(state: AgentState) -> Literal["tool", "complex", "simple"]:
    if state.query_class == "tool":
        return "tool"
    # 定义题（单 IE/术语含义）几乎总被 classify 判 complexity=simple，于是落到单 query、
    # 无 hyde/multi_query 的 simple 路径——召回最弱，恰恰答不全那唯一的权威定义条款。
    # 让 query_class=definition 也走 complex 扩展链（hyde + multi_query）提升召回。
    if state.complexity == "complex" or state.query_class == "definition":
        return "complex"
    return "simple"


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
