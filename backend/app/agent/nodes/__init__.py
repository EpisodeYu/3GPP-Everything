"""Agent 节点（每个节点纯函数：`state, *, deps -> dict[partial update]`）。

节点签名约定：
    async def some_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]

返回的 dict 是 LangGraph 状态的 partial update（带 reducer 的字段会走 reducer，
其它字段直接覆盖）。这样：
- 单测可直接 `await some_node(state, deps=stub_deps)` 不依赖 graph
- graph 编译时用 `functools.partial(some_node, deps=deps)` 绑定
"""

from .classify import classify_node
from .generate import generate_node, parse_citations
from .rerank import rerank_node
from .retrieve import retrieve_node
from .rewrite import rewrite_node
from .self_rag import self_rag_node

__all__ = [
    "classify_node",
    "generate_node",
    "parse_citations",
    "rerank_node",
    "retrieve_node",
    "rewrite_node",
    "self_rag_node",
]
