"""retrieve 节点：dense + BM25 sparse + RRF。

口径见 `docs/03-development/03-agent.md §4.5`。

- dense 走 `DenseRetriever.retrieve()`；sparse 走 `SparseRetriever.retrieve()` (sync)，
  在 `asyncio.to_thread` 里跑避免阻塞 event loop
- 多个 query 串行检索（M4.2 simple 路径只 1 条；M4.3 multi_query 时再考虑并发）
- 用 RRF 融合并排重，截断到 settings.RETRIEVAL_FINAL_TOP_K（默认 50）
- 缓存：`{prefix}:retrieve:{sha256(queries+spec_filter)}` TTL 1h；命中直接还原
- 节点产出后通过 `get_stream_writer()` 发自定义事件 `chunks_hit`，供
  backend 在 `astream_events`/`astream(stream_mode="custom")` 里转成 SSE 事件
  （口径见 `docs/03-development/03-agent.md §7`）
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk
from app.core.errors import RetrievalError
from app.retrieval.hybrid import rrf_merge
from app.retrieval.models import RetrievedChunk

log = logging.getLogger(__name__)


async def retrieve_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    s = deps.settings
    base_queries: list[str] = list(state.rewritten_queries) if state.rewritten_queries else []
    if not base_queries and state.user_input:
        base_queries = [state.user_input]
    base_queries = [q for q in (q.strip() for q in base_queries) if q]

    # map-reduce 触发：仅 complex 非 definition 且子查询 > 1（口径见
    # docs/04-handoff/2026-06-02-mapreduce-retrieval-plan.md §2.4）。其余路径走下方
    # 现行 single-pool 逻辑，向后兼容。
    if (
        s.RETRIEVAL_MAPREDUCE_ENABLED
        and state.complexity == "complex"
        and state.query_class != "definition"
        and len(base_queries) > 1
    ):
        return await _mapreduce_retrieve(state, deps, base_queries)

    # ---- single-pool（现行逻辑）----
    queries = list(base_queries)
    if state.hyde_doc and state.hyde_doc.strip():
        queries.append(state.hyde_doc.strip())
    if not queries:
        return {"candidates": []}

    cache_payload = {"queries": queries, "spec_filter": None}

    if deps.cache is not None:
        cached = await deps.cache.get("retrieve", cache_payload)
        if cached:
            chunks = [StateChunk.model_validate(c) for c in cached]
            log.debug("retrieve_node cache hit (%d chunks)", len(chunks))
            # F-3：cache hit 路径也要 emit chunks_hit，否则 SSE 事件序列在
            # 第二次同样问法时少一条事件（rerank 总会 emit chunks_rerank，retrieve
            # 不能漏）。前端按 chunks_hit + chunks_rerank 两次更新候选展示。
            await _emit_chunks_hit(chunks)
            return {"candidates": chunks}

    dense_lists: list[list[RetrievedChunk]] = []
    sparse_lists: list[list[RetrievedChunk]] = []

    for q in queries:
        d, sp = await _fetch_dense_sparse(deps, q, s)
        dense_lists.append(d)
        if deps.sparse is not None:
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

    await _emit_chunks_hit(state_chunks)
    return {"candidates": state_chunks}


async def _fetch_dense_sparse(
    deps: AgentDeps, query: str, s: Any
) -> tuple[list[RetrievedChunk], list[RetrievedChunk]]:
    """单条 query 的 dense + sparse 检索；任一侧失败退空列表不阻塞主路径。

    sparse 是 sync（bm25s），在 `asyncio.to_thread` 里跑避免阻塞 event loop。
    sparse 未接（deps.sparse is None）时返回空 sparse 列表。
    """
    try:
        dense = await deps.dense.retrieve(query, top_k=s.RETRIEVAL_DENSE_TOP_K)
    except RetrievalError as exc:
        log.warning("retrieve_node dense failed for %r: %s", query, exc)
        dense = []

    sparse: list[RetrievedChunk] = []
    if deps.sparse is not None:
        try:
            sparse = await asyncio.to_thread(
                deps.sparse.retrieve, query, top_k=s.RETRIEVAL_SPARSE_TOP_K
            )
        except RetrievalError as exc:
            log.warning("retrieve_node sparse failed for %r: %s", query, exc)
            sparse = []
    return dense, sparse


async def _mapreduce_retrieve(
    state: AgentState, deps: AgentDeps, base_queries: list[str]
) -> dict[str, Any]:
    """map-reduce 检索的 map 阶段：每个子查询独立 retrieve → 各自候选池。

    - 每个 `base_queries[i]` 成一个 facet：dense+sparse → RRF → top
      `RETRIEVAL_MAPREDUCE_PER_QUERY_POOL` → `candidates_by_query[i]`。
    - `hyde_doc` 是"理想答案"不是"角度"，**不单独成 facet**，只作为额外召回信号汇入
      flat 池（给 SSE chunks_hit / generate fallback / 缓存用）。
    - flat `candidates` = 全部 dense/sparse（含 hyde）再 RRF 一次，语义同 single-pool。

    rerank_node 见到非空 `candidates_by_query` → 走 per-query 重排 + 轮转合并。
    """
    s = deps.settings
    cache_payload = {"queries": base_queries, "spec_filter": None, "mode": "mapreduce"}

    if deps.cache is not None:
        cached = await deps.cache.get("retrieve", cache_payload)
        if cached and isinstance(cached, dict):
            cached_flat = [StateChunk.model_validate(c) for c in cached.get("flat", [])]
            cached_by_query = [
                [StateChunk.model_validate(c) for c in lst] for lst in cached.get("by_query", [])
            ]
            log.debug("retrieve_node mapreduce cache hit (%d facets)", len(cached_by_query))
            await _emit_chunks_hit(cached_flat)
            return {"candidates": cached_flat, "candidates_by_query": cached_by_query}

    all_dense: list[list[RetrievedChunk]] = []
    all_sparse: list[list[RetrievedChunk]] = []
    by_query: list[list[StateChunk]] = []

    for q in base_queries:
        dense, sparse = await _fetch_dense_sparse(deps, q, s)
        all_dense.append(dense)
        all_sparse.append(sparse)
        pool = rrf_merge(
            dense,
            sparse,
            k=s.RETRIEVAL_RRF_K,
            top_n=s.RETRIEVAL_MAPREDUCE_PER_QUERY_POOL,
        )
        by_query.append([StateChunk.from_retrieval(c) for c in pool])

    if state.hyde_doc and state.hyde_doc.strip():
        dense, sparse = await _fetch_dense_sparse(deps, state.hyde_doc.strip(), s)
        all_dense.append(dense)
        all_sparse.append(sparse)

    flat = rrf_merge(*all_dense, *all_sparse, k=s.RETRIEVAL_RRF_K, top_n=s.RETRIEVAL_FINAL_TOP_K)
    flat_chunks = [StateChunk.from_retrieval(c) for c in flat]

    if deps.cache is not None and flat_chunks:
        try:
            await deps.cache.set(
                "retrieve",
                cache_payload,
                {
                    "flat": [c.model_dump(mode="json") for c in flat_chunks],
                    "by_query": [[c.model_dump(mode="json") for c in lst] for lst in by_query],
                },
            )
        except Exception as exc:
            log.warning("retrieve_node mapreduce cache.set failed: %s", exc)

    await _emit_chunks_hit(flat_chunks)
    return {"candidates": flat_chunks, "candidates_by_query": by_query}


async def _emit_chunks_hit(chunks: list[StateChunk]) -> None:
    """节点边界 emit `chunks_hit` 自定义事件。

    通过两条通道一起 emit（口径 §7）：
    - `get_stream_writer()` → 落入 `astream(stream_mode="custom")` 流，backend SSE
      路径主要走这条
    - `adispatch_custom_event` → 落入 `astream_events(v="v2")` 流的
      `on_custom_event`，给 backend 在统一 `astream_events` 通道里也能拿到

    任一通道在当前 graph 上下文里不可用（单测直接 `await retrieve_node(...)`）抛
    RuntimeError 吞掉，不影响主路径。Payload 截 top-10 + preview 240 字，避免 SSE 帧过大。
    """
    payload = [
        {
            "chunk_id": c.chunk_id,
            "spec_id": c.spec_id,
            "section_path": ".".join(c.section_path),
            "section_title": c.section_title,
            "score": c.fused_score,
            # `preview` (240 字) 给前端流式展示；`content` 给 eval runner 拼
            # 完整 contexts 用（Langfuse Cloud faithfulness evaluator 需要）。
            "preview": (c.content or "")[:240],
            "content": c.content or "",
        }
        for c in chunks[:10]
    ]
    event = {"type": "chunks_hit", "chunks": payload}

    with contextlib.suppress(RuntimeError):
        writer = get_stream_writer()
        writer(event)

    with contextlib.suppress(RuntimeError):
        # 不在 callback 上下文里（单测直接 await retrieve_node(...)）时抛 RuntimeError
        await adispatch_custom_event("chunks_hit", event)
