"""Agent 依赖容器（DI bundle）。

LangGraph 节点是 `state -> state` 的函数，但每个节点需要 LLM client / 检索器 /
reranker / 缓存等"长生命周期"对象。把它们打包成 `AgentDeps` 一次构造、注入到
`build_simple_graph(deps)` 的闭包里；测试可注入 stub。

为何不直接用 `RunnableConfig.configurable`：M4.5 Langfuse / checkpoint 接入后
config 字段会比较密；显式 `AgentDeps` 让节点签名清楚（`async def f(state, *, deps)`）
而不是从 config dict 里挖。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.utils.history_compactor import RedisLike
from app.core.config import Settings, get_settings
from app.db.base import get_sessionmaker
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
    # redis：compact_history 节点的 summary 缓存（key tgpp:cache:history_summary:...）。
    # 仅 get/setex 两个接口（RedisLike）；None → 不缓存（每个长会话回合重算 summary）。
    redis: RedisLike | None = None
    db_sessionmaker: async_sessionmaker[AsyncSession] | None = None
    settings: Settings = field(default_factory=get_settings)

    @classmethod
    def from_env(cls) -> AgentDeps:
        """生产配置：从 .env 一次性构造完整依赖。caller 持有 deps，进程退出时 close。"""
        s = get_settings()
        litellm = LiteLLMClient(settings=s)
        dense = DenseRetriever.from_env(embedder=litellm, settings=s)
        sparse = SparseRetriever.from_env(settings=s)
        # RERANK_ENABLED=false → reranker 置 None；rerank 节点对 None 已有降级
        # （退回 fused/RRF 排序，不调任何 rerank 上游）。给无 Voyage 的部署用。
        reranker = (
            Reranker.from_env(litellm_client=litellm, settings=s) if s.RERANK_ENABLED else None
        )
        cache = RetrievalCache(settings=s)
        # summary 缓存用独立 redis 句柄（decode_responses=True，与 history_compactor
        # 期望的 str 值一致）；连不上不阻塞——compact_history 对 get/setex 异常已降级。
        redis = _build_redis(s)
        return cls(
            llm=litellm,
            dense=dense,
            sparse=sparse,
            reranker=reranker,
            cache=cache,
            redis=redis,
            db_sessionmaker=get_sessionmaker(),
            settings=s,
        )

    async def aclose(self) -> None:
        """与 from_env 配套；测试自行管理 stub 生命周期。"""
        if hasattr(self.dense, "close"):
            await self.dense.close()
        if self.cache is not None:
            await self.cache.close()
        aclose = getattr(self.redis, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
        await self.llm.close()


def _build_redis(settings: Settings) -> Any | None:
    """构造 summary 缓存用的 aioredis 句柄；任何失败返回 None（不阻塞 agent 启动）。"""
    try:
        import redis.asyncio as aioredis

        return aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None
