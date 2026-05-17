"""retrieve 节点：dense + BM25 sparse + RRF。

口径见 `docs/03-development/03-agent.md §4.5`。

- dense 走 `DenseRetriever.retrieve()`；sparse 走 `SparseRetriever.retrieve()` (sync)，
  在 `asyncio.to_thread` 里跑避免阻塞 event loop
- 多个 query 串行检索（M4.2 simple 路径只 1 条；M4.3 multi_query 时再考虑并发）
- 用 RRF 融合并排重，截断到 settings.RETRIEVAL_FINAL_TOP_K（默认 50）
- 缓存：`{prefix}:retrieve:{sha256(queries+spec_filter)}` TTL 1h；命中直接还原
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langgraph.errors import NodeInterrupt

from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk
from app.core.errors import RetrievalError
from app.retrieval.hybrid import rrf_merge
from app.retrieval.models import RetrievedChunk

log = logging.getLogger(__name__)


async def retrieve_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        raise NodeInterrupt("cancelled by user")
    if state.paused:
        raise NodeInterrupt("paused by user")

    queries: list[str] = list(state.rewritten_queries) if state.rewritten_queries else []
    if not queries and state.user_input:
        queries = [state.user_input]
    if state.hyde_doc:
        queries.append(state.hyde_doc)
    queries = [q for q in (q.strip() for q in queries) if q]
    if not queries:
        return {"candidates": []}

    s = deps.settings
    cache_payload = {"queries": queries, "spec_filter": None}

    if deps.cache is not None:
        cached = await deps.cache.get("retrieve", cache_payload)
        if cached:
            chunks = [StateChunk.model_validate(c) for c in cached]
            log.debug("retrieve_node cache hit (%d chunks)", len(chunks))
            return {"candidates": chunks}

    dense_lists: list[list[RetrievedChunk]] = []
    sparse_lists: list[list[RetrievedChunk]] = []

    for q in queries:
        try:
            d = await deps.dense.retrieve(q, top_k=s.RETRIEVAL_DENSE_TOP_K)
        except RetrievalError as exc:
            log.warning("retrieve_node dense failed for %r: %s", q, exc)
            d = []
        dense_lists.append(d)

        if deps.sparse is not None:
            try:
                sp = await asyncio.to_thread(
                    deps.sparse.retrieve, q, top_k=s.RETRIEVAL_SPARSE_TOP_K
                )
            except RetrievalError as exc:
                log.warning("retrieve_node sparse failed for %r: %s", q, exc)
                sp = []
            sparse_lists.append(sp)

    fused = rrf_merge(
        *dense_lists,
        *sparse_lists,
        k=s.RETRIEVAL_RRF_K,
        top_n=s.RETRIEVAL_FINAL_TOP_K,
    )

    state_chunks = [StateChunk.from_retrieval(c) for c in fused]

    if deps.cache is not None and state_chunks:
        try:
            await deps.cache.set(
                "retrieve",
                cache_payload,
                [c.model_dump(mode="json") for c in state_chunks],
            )
        except Exception as exc:
            log.warning("retrieve_node cache.set failed: %s", exc)

    return {"candidates": state_chunks}
