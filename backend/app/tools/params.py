"""Params 工具：IE / 字段查询（"X 字段在哪些 spec 出现过"）。

口径见 `docs/03-development/03-agent.md §4.9 params`。走 BM25 全文检索，限定
`chunk_type ∈ {text, table}`。

接口：
    async def params_tool(state, *, deps) -> dict[str, Any]

返回结构（写入 `state.tool_results["params"]`）：
    {
        "query": str,
        "hits": [
            {"chunk_id", "spec_id", "section_path", "chunk_type", "score", "preview"},
            ...
        ],
        "warning": str | None,
    }

实现：复用 `deps.sparse`（BM25）。返回前 `_TOP_K` 个 chunk_type ∈ {text, table} 命中。
deps.sparse 为 None（测试 / BM25 未加载）时返回 warning。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agent.deps import AgentDeps
from app.agent.state import AgentState

log = logging.getLogger(__name__)

_TOP_K = 10
_BM25_TOP_K = 60
_ALLOWED_TYPES = {"text", "table"}
_PREVIEW_CHARS = 240


async def params_tool(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    query = (
        (state.rewritten_queries[0] if state.rewritten_queries else state.user_input) or ""
    ).strip()
    if not query:
        return {"query": "", "hits": [], "warning": "empty query"}

    if deps.sparse is None:
        return {"query": query, "hits": [], "warning": "sparse retriever unavailable"}

    try:
        raw = await asyncio.to_thread(deps.sparse.retrieve, query, top_k=_BM25_TOP_K)
    except Exception as exc:
        log.warning("params_tool sparse retrieve failed: %s", exc)
        return {"query": query, "hits": [], "warning": f"sparse error: {exc}"}

    hits: list[dict[str, Any]] = []
    for c in raw:
        if c.chunk_type not in _ALLOWED_TYPES:
            continue
        hits.append(
            {
                "chunk_id": c.chunk_id,
                "spec_id": c.spec_id,
                "section_path": list(c.section_path),
                "chunk_type": c.chunk_type,
                "score": c.score_sparse or 0.0,
                "preview": (c.content or "")[:_PREVIEW_CHARS],
            }
        )
        if len(hits) >= _TOP_K:
            break
    return {"query": query, "hits": hits, "warning": None}
