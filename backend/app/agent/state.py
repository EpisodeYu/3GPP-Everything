"""LangGraph AgentState（Pydantic v2）。

口径 = `docs/03-development/03-agent.md §2`。M4.2 落最小可用字段，complex
分支字段（hyde_doc / multi_query / retry_count）保留为默认值，等 M4.3 接管。

`RetrievedChunk` 镜像 `app.retrieval.models.RetrievedChunk`：retrieval 层用
`@dataclass(slots=True)`（hot path 友好）；agent 层走 LangGraph state，必须可被
pydantic 序列化进 PostgresSaver checkpoint，所以这里用 `pydantic.BaseModel` 重声明。
两边字段名一一对应，转换在 `state.RetrievedChunk.from_retrieval()` 完成。

多轮上下文承载（2026-06-02 接通真多轮后修订）：
- `raw_history`：**chat 路由从 PG 重载的未压缩 prior 历史**（已排除本轮当前问题与
  assistant stub），每条 `{"id", "role", "content"}`。路由只负责持久化加载；compaction
  本身改由图内 `compact_history` 节点经 `deps.llm` + `deps.redis` 完成（§6.1 约定：
  「由 build_graph 的 deps 注入」），不再在路由里调 `compact_history`。
- `history`：`compact_history` 节点产出的**已压缩**历史（`[summary?, 最近 N 条]`），
  普通字段、覆盖语义（无 reducer），contextualize / generate 节点消费它。之所以不用
  `messages`：生产挂 `AsyncPostgresSaver`（thread_id=session_id）时，`add_messages`
  reducer 会把每轮重载的全量历史**跨 checkpoint 累积**（fresh message id 无法去重）→
  重复膨胀。普通字段 LastValue channel 每轮被 input/节点覆盖，干净可预测。
- `messages`：保留 `add_messages` reducer 字段以兼容老 checkpoint 反序列化与 §2 文档，
  **不再被 chat 路由写入、也不再被任何节点消费**（接通历史改走 `raw_history`/`history`）。
- `contextualized_input`：contextualize 节点把追问的指代/省略补全成自包含问题后写这里；
  classify / rewrite / hyde / self_rag 经 `effective_query` 优先读它。
- `session_id`：仅供 `compact_history` 节点拼 summary 缓存 key（thread_id 同值）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.retrieval.models import RetrievedChunk as _RetrievalChunk

ChunkType = Literal["text", "table", "formula", "figure"]
QueryClass = Literal["definition", "procedure", "tool", "unknown"]
Complexity = Literal["simple", "complex"]
# raw_lookup 模式已下线，仅保留 qa。老 session/checkpoint 里残留的 'raw_lookup'
# 由 AgentState.mode 的 before-validator 归一成 'qa'。
Mode = Literal["qa"]
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
    # raw_history：路由从 PG 重载的未压缩 prior 历史（已排除本轮当前问），每条带 id；
    # 仅 compact_history 节点消费它（reconstruct → compact_history()）。
    # 每条 = {"id": <uuid str>, "role": "user"|"assistant"|"system", "content": ...}。
    raw_history: list[dict[str, str]] = Field(default_factory=list)
    # history：compact_history 节点产出的已压缩历史（节点消费的就是它），普通字段=覆盖语义。
    # 每条 = {"role": "user"|"assistant"|"system", "content": ...}。
    history: list[dict[str, str]] = Field(default_factory=list)
    # session_id：仅供 compact_history 节点拼 summary 缓存 key（= thread_id）。
    session_id: str | None = None
    # contextualized_input：追问指代消解后的自包含问题；空则下游回退到 user_input。
    contextualized_input: str = ""
    # messages：legacy（add_messages reducer）。保留兼容老 checkpoint，不再写/读。见类 docstring。
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

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_legacy_mode(cls, v: object) -> str:
        # 老 session/checkpoint 残留的 'raw_lookup' 等非 qa 值归一为 'qa'。
        return "qa"

    @property
    def effective_query(self) -> str:
        """检索 / 分类 / 改写用的问题文本。

        多轮追问被 contextualize 节点消解成自包含问题后写 `contextualized_input`，
        优先用它；首轮（或 contextualize 未触发）回退到原始 `user_input`。
        generate 仍回答原始 `user_input`（口径见 03-agent.md §6.1）。
        """
        return self.contextualized_input or self.user_input
