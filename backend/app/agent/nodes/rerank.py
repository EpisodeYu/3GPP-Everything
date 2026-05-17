"""rerank 节点：voyage rerank-2.5 → top-K reranked。

口径见 `docs/03-development/03-agent.md §4.6`。

- 输入：`state.candidates`（hybrid 融合后 top-50）
- 输出：`state.reranked`（按 `score_rerank` 降序的 top-K，K = settings.RERANK_TOP_K）
- 失败处理：rerank 上游失败时，退回 `fused_score` 排序的 top-K，**不阻塞**主路径
- 若 deps.reranker 为 None（M4.2 早期 / 离线模式），同样退回 fused 排序
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.errors import NodeInterrupt

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
        raise NodeInterrupt("cancelled by user")
    if state.paused:
        raise NodeInterrupt("paused by user")

    if not state.candidates:
        return {"reranked": []}

    s = deps.settings
    top_k = s.RERANK_TOP_K
    query = state.rewritten_queries[0] if state.rewritten_queries else state.user_input
    query = (query or "").strip()

    if not query or deps.reranker is None:
        # 无 query 或没接 reranker → 退回 fused 排序的 top-K
        return {
            "reranked": _fused_top_k(state.candidates, top_k=top_k),
        }

    cands = [_state_to_retrieval(c) for c in state.candidates]
    try:
        reranked = await deps.reranker.rerank(query, cands, top_k=top_k)
    except RetrievalError as exc:
        log.warning("rerank_node failed, fallback to fused order: %s", exc)
        return {"reranked": _fused_top_k(state.candidates, top_k=top_k)}

    out = [StateChunk.from_retrieval(c) for c in reranked]
    return {"reranked": out}


def _fused_top_k(candidates: list[StateChunk], *, top_k: int) -> list[StateChunk]:
    return sorted(candidates, key=lambda c: c.fused_score, reverse=True)[:top_k]
