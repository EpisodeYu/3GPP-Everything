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
import re
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

# 定义题专用：section_title 命中查询里 IE/专名 token 时给的加性 rerank 提升量。
# Voyage relevance_score ∈ [0,1]，0.1 足以把"标题就是该 IE"的定义条款顶到测试规范
# 提及之上，又不至于完全压过强相关命中。**启发式，待 eval 调**（见 03-agent.md §4.6）。
_DEFINITION_TITLE_BOOST = 0.1

# IE / 信令消息 / 字段名通常是 hyphenated（`PDSCH-Config`、`p-ZP-CSI-RS-...`）或
# CamelCase / 全大写缩写（`RRCReconfiguration`、`AMF`）。从 query 抽这类"专名 token"，
# 用来和 chunk 的 section_title 做命中匹配。
_IE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+|[A-Z][A-Za-z0-9]{2,}")


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
    is_definition = state.query_class == "definition"
    # 定义题：先取更宽的 rerank 结果（Voyage 按候选数计费，top_k 只截断返回，放宽免费），
    # 再用 section_title 命中 boost 把"标题即该 IE"的定义条款顶上来，最后截到 top_k。
    pool_k = len(cands) if is_definition else top_k
    try:
        reranked = await deps.reranker.rerank(query, cands, top_k=pool_k)
    except RetrievalError as exc:
        log.warning("rerank_node failed, fallback to fused order: %s", exc)
        out = _fused_top_k(state.candidates, top_k=top_k)
        await _emit_chunks_rerank(out)
        return {"reranked": out}

    ranked = [StateChunk.from_retrieval(c) for c in reranked]
    if is_definition:
        out = _definition_boost(ranked, query, weight=_DEFINITION_TITLE_BOOST, top_k=top_k)
    else:
        out = ranked
    await _emit_chunks_rerank(out)
    return {"reranked": out}


def _fused_top_k(candidates: list[StateChunk], *, top_k: int) -> list[StateChunk]:
    return sorted(candidates, key=lambda c: c.fused_score, reverse=True)[:top_k]


def _salient_terms(query: str) -> list[str]:
    """从 query 抽 IE/专名 token（小写化），用于 section_title 命中匹配。"""
    return [t.lower() for t in _IE_TOKEN_RE.findall(query or "")]


def _definition_boost(
    chunks: list[StateChunk], query: str, *, weight: float, top_k: int
) -> list[StateChunk]:
    """定义题专用重排：section_title 命中 query 里 IE/专名 token 的 chunk 上调分数。

    base 取 score_rerank（无则退 fused_score）；命中标题 +weight 后整体降序取 top_k。
    query 抽不出专名 token 时不动原序，直接截断。稳定排序保证同分维持 rerank 原次序。
    """
    terms = _salient_terms(query)
    if not terms:
        return chunks[:top_k]

    def _adjusted(c: StateChunk) -> float:
        base = c.score_rerank if c.score_rerank is not None else (c.fused_score or 0.0)
        title = (c.section_title or "").lower()
        return base + (weight if any(t in title for t in terms) else 0.0)

    return sorted(chunks, key=_adjusted, reverse=True)[:top_k]


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
            # `preview` (240 字) 给前端流式展示用；`content` 是完整 chunk 文本，
            # eval runner（langfuse experiment）拼 trace.output.contexts 给
            # Cloud-side faithfulness evaluator 当 `{{context}}` 用，必须完整。
            "preview": (c.content or "")[:240],
            "content": c.content or "",
        }
        for c in chunks
    ]
    event = {"type": "chunks_rerank", "chunks": payload}

    with contextlib.suppress(RuntimeError):
        writer = get_stream_writer()
        writer(event)

    with contextlib.suppress(RuntimeError):
        await adispatch_custom_event("chunks_rerank", event)
