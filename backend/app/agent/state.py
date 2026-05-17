"""LangGraph AgentState（Pydantic v2）。

口径 = `docs/03-development/03-agent.md §2`。M4.2 落最小可用字段，complex
分支字段（hyde_doc / multi_query / retry_count）保留为默认值，等 M4.3 接管。

`RetrievedChunk` 镜像 `app.retrieval.models.RetrievedChunk`：retrieval 层用
`@dataclass(slots=True)`（hot path 友好）；agent 层走 LangGraph state，必须可被
pydantic 序列化进 PostgresSaver checkpoint，所以这里用 `pydantic.BaseModel` 重声明。
两边字段名一一对应，转换在 `state.RetrievedChunk.from_retrieval()` 完成。

`messages` 用 `Annotated[list[BaseMessage], add_messages]` reducer，多轮自然累积；
M4.2 simple 单轮也工作（list 单条），M4.5 加 PostgresSaver 后无缝多轮。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field

from app.retrieval.models import RetrievedChunk as _RetrievalChunk

ChunkType = Literal["text", "table", "formula", "figure"]
QueryClass = Literal["definition", "procedure", "tool", "unknown"]
Complexity = Literal["simple", "complex"]
Mode = Literal["qa", "raw_lookup"]
SelfRagVerdict = Literal["accept", "retry", "insufficient", "unknown"]


class RetrievedChunk(BaseModel):
    """Agent 状态里携带的 chunk；与 `app.retrieval.models.RetrievedChunk` 字段对齐。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    spec_id: str
    section_path: tuple[str, ...] = ()
    section_title: str = ""
    chunk_type: str = "text"
    content: str = ""
    score_dense: float | None = None
    score_sparse: float | None = None
    score_rerank: float | None = None
    fused_score: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_retrieval(cls, c: _RetrievalChunk) -> RetrievedChunk:
        return cls(
            chunk_id=c.chunk_id,
            spec_id=c.spec_id,
            section_path=tuple(c.section_path),
            section_title=c.section_title,
            chunk_type=c.chunk_type,
            content=c.content,
            score_dense=c.score_dense,
            score_sparse=c.score_sparse,
            score_rerank=c.score_rerank,
            fused_score=c.fused_score,
            extra=dict(c.extra),
        )


class AgentState(BaseModel):
    """LangGraph 主干状态。

    pydantic v2 BaseModel 直接喂 LangGraph，节点返回 `dict[field, value]` 做 partial
    update；带 reducer 的字段（`messages`）由 reducer 合并，其它字段直接覆盖。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    # === 输入 ===
    user_input: str = ""
    user_language: Literal["zh", "en"] = "en"
    mode: Mode = "qa"
    explicit_tools: list[str] = Field(default_factory=list)

    # === 多轮上下文 ===
    messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)

    # === 路由 ===
    query_class: QueryClass | None = None
    complexity: Complexity = "simple"

    # === 查询改写 ===
    rewritten_queries: list[str] = Field(default_factory=list)
    hyde_doc: str | None = None

    # === 检索 ===
    candidates: list[RetrievedChunk] = Field(default_factory=list)
    reranked: list[RetrievedChunk] = Field(default_factory=list)

    # === 工具 ===
    tool_results: dict[str, Any] = Field(default_factory=dict)

    # === 生成 ===
    final_answer: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0

    # === 自校验 ===
    self_rag_verdict: SelfRagVerdict | None = None
    self_rag_missing: list[str] = Field(default_factory=list)
    retry_count: int = 0

    # === 监控 / 控制 ===
    trace_id: str | None = None
    cancelled: bool = False
    paused: bool = False
    run_id: str | None = None
