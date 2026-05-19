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
            # F-3：cache hit 路径也要 emit chunks_hit，否则 SSE 事件序列在
            # 第二次同样问法时少一条事件（rerank 总会 emit chunks_rerank，retrieve
            # 不能漏）。前端按 chunks_hit + chunks_rerank 两次更新候选展示。
            await _emit_chunks_hit(chunks)
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

    await _emit_chunks_hit(state_chunks)
    return {"candidates": state_chunks}


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
            "preview": (c.content or "")[:240],
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
