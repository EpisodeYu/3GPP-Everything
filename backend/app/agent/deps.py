"""Agent 依赖容器（DI bundle）。

LangGraph 节点是 `state -> state` 的函数，但每个节点需要 LLM client / 检索器 /
reranker / 缓存等"长生命周期"对象。把它们打包成 `AgentDeps` 一次构造、注入到
`build_simple_graph(deps)` 的闭包里；测试可注入 stub。

为何不直接用 `RunnableConfig.configurable`：M4.5 Langfuse / checkpoint 接入后
config 字段会比较密；显式 `AgentDeps` 让节点签名清楚（`async def f(state, *, deps)`）
而不是从 config dict 里挖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import Settings, get_settings
from app.llm.litellm_client import LiteLLMClient
from app.retrieval.cache import RetrievalCache
from app.retrieval.dense import DenseRetriever
from app.retrieval.models import RetrievedChunk as _RetrievalChunk
from app.retrieval.rerank import Reranker
from app.retrieval.sparse import SparseRetriever


class _DenseRetrieverProto(Protocol):
    async def retrieve(
        self, query: str, *, top_k: int = ..., filter_spec_ids: list[str] | None = ...
    ) -> list[_RetrievalChunk]: ...


class _SparseRetrieverProto(Protocol):
    def retrieve(self, query: str, *, top_k: int = ...) -> list[_RetrievalChunk]: ...


class _RerankerProto(Protocol):
    async def rerank(
        self, query: str, candidates: list[_RetrievalChunk], *, top_k: int = ...
    ) -> list[_RetrievalChunk]: ...


@dataclass
class AgentDeps:
    """Agent 节点共享的依赖。所有字段都用 Protocol，便于测试用 stub 替换。"""

    llm: LiteLLMClient
    dense: _DenseRetrieverProto
    sparse: _SparseRetrieverProto | None = None
    reranker: _RerankerProto | None = None
    cache: RetrievalCache | None = None
    settings: Settings = field(default_factory=get_settings)

    @classmethod
    def from_env(cls) -> AgentDeps:
        """生产配置：从 .env 一次性构造完整依赖。caller 持有 deps，进程退出时 close。"""
        s = get_settings()
        litellm = LiteLLMClient(settings=s)
        dense = DenseRetriever.from_env(embedder=litellm, settings=s)
        sparse = SparseRetriever.from_env(settings=s)
        reranker = Reranker.from_env(litellm_client=litellm, settings=s)
        cache = RetrievalCache(settings=s)
        return cls(
            llm=litellm,
            dense=dense,
            sparse=sparse,
            reranker=reranker,
            cache=cache,
            settings=s,
        )

    async def aclose(self) -> None:
        """与 from_env 配套；测试自行管理 stub 生命周期。"""
        if hasattr(self.dense, "close"):
            await self.dense.close()
        if self.cache is not None:
            await self.cache.close()
        await self.llm.close()
