"""rerank 节点：voyage rerank-2.5 → top-K reranked。

口径见 `docs/03-development/03-agent.md §4.6` + §7。

- 输入：`state.candidates`（hybrid 融合后 top-50）
- 输出：`state.reranked`（按 `score_rerank` 降序的 top-K，K = settings.RERANK_TOP_K）
- 失败处理：rerank 上游失败时，退回 `fused_score` 排序的 top-K，**不阻塞**主路径
- 若 deps.reranker 为 None（M4.2 早期 / 离线模式），同样退回 fused 排序
- M4.7 Q6/Q7：节点产出后通过 `get_stream_writer()` + `adispatch_custom_event` 发
  `chunks_rerank` 事件（top-K + rerank_score），与 retrieve 的 `chunks_hit` 区分；
  前端用 `chunks_rerank` 覆盖 `chunks_hit` 的初始候选展示。
"""

from __future__ import annotations

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
from app.retrieval.models import RetrievedChunk as RetrievalChunk

log = logging.getLogger(__name__)


def _state_to_retrieval(c: StateChunk) -> RetrievalChunk:
    return RetrievalChunk(
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


async def rerank_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    if not state.candidates:
        return {"reranked": []}

    s = deps.settings
    top_k = s.RERANK_TOP_K
    query = state.rewritten_queries[0] if state.rewritten_queries else state.user_input
    query = (query or "").strip()

    if not query or deps.reranker is None:
        # 无 query 或没接 reranker → 退回 fused 排序的 top-K
        out = _fused_top_k(state.candidates, top_k=top_k)
        await _emit_chunks_rerank(out)
        return {"reranked": out}

    cands = [_state_to_retrieval(c) for c in state.candidates]
    try:
        reranked = await deps.reranker.rerank(query, cands, top_k=top_k)
    except RetrievalError as exc:
        log.warning("rerank_node failed, fallback to fused order: %s", exc)
        out = _fused_top_k(state.candidates, top_k=top_k)
        await _emit_chunks_rerank(out)
        return {"reranked": out}

    out = [StateChunk.from_retrieval(c) for c in reranked]
    await _emit_chunks_rerank(out)
    return {"reranked": out}


def _fused_top_k(candidates: list[StateChunk], *, top_k: int) -> list[StateChunk]:
    return sorted(candidates, key=lambda c: c.fused_score, reverse=True)[:top_k]


async def _emit_chunks_rerank(chunks: list[StateChunk]) -> None:
    """节点边界 emit `chunks_rerank` 自定义事件（M4.7 Q6/Q7）。

    与 retrieve 的 `chunks_hit` 区别：
    - chunks_hit：top-50 候选，无 rerank_score
    - chunks_rerank：top-K 重排后，含 rerank_score（fallback 路径用 fused_score 作为 surrogate）

    双通道 emit（同 retrieve）：stream_writer（custom mode）+ adispatch_custom_event（events v2）。
    单测 `await rerank_node(...)` 直调时两通道都抛 RuntimeError，被 suppress 吞掉。
    """
    payload = [
        {
            "chunk_id": c.chunk_id,
            "spec_id": c.spec_id,
            "section_path": ".".join(c.section_path),
            "section_title": c.section_title,
            "rerank_score": c.score_rerank if c.score_rerank is not None else c.fused_score,
            "preview": (c.content or "")[:240],
        }
        for c in chunks
    ]
    event = {"type": "chunks_rerank", "chunks": payload}

    with contextlib.suppress(RuntimeError):
        writer = get_stream_writer()
        writer(event)

    with contextlib.suppress(RuntimeError):
        await adispatch_custom_event("chunks_rerank", event)
