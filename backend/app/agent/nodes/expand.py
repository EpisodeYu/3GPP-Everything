"""expand 节点（small2big 召回侧扩段，Issue #3）。

口径见 `docs/03-development/03-agent.md §4.6b` + issue #3 方案。

位置：rerank 之后、generate 之前。把 rerank 后的命中小块按 `parent_section_id`
回扩为整段 section 喂给 LLM（small-to-big）：

- 命中小块的 `parent_section_id` 已在 `extra`（dense/sparse 都把非核心 payload 塞进
  extra）。
- 兄弟块**顺序**来自 PG `chunks_meta`（`document_order`，parent_section_id 有索引）；
  `chunks_meta` 不存 content，content 从 **Qdrant** 批量取（point id = chunk_id），
  与 reader `docs.py::_fetch_content_map` 同一成熟模式。
- 预算护栏：某段 `parent_section_chars > SMALL2BIG_MAX_SECTION_CHARS` → 退化为命中块
  `document_order` 前后各 N 个兄弟块（`SMALL2BIG_NEIGHBOR_WINDOW`）；否则取整段。
- 全局 `SMALL2BIG_TOTAL_BUDGET_CHARS`：按 rerank 名次累计扩段字符，超出后靠后的块不再扩
  （保留小块），护住 generate prompt 的 token 成本 / 延迟。
- **同 parent 只扩一次**：挂到该 parent 名次最高的块，其余同 parent 块保留原小块，避免
  整段重复占 context / 引用位。

失败与降级（不阻塞主路径）：
- `SMALL2BIG_ENABLED=False` / `reranked` 空 / `deps.db_sessionmaker is None` → 直接透传。
- PG / Qdrant 任一步异常 → 该批不扩，log warning，reranked 原样返回。

产出：把 `expanded_content` 写到被扩块的 `state.reranked[i]`（generate 的 `_chunk_view`
优先喂 `expanded_content or content`），并通过双通道 emit `chunks_expanded` 事件（供
backend SSE 转发给前端 / eval 拼真实上下文）。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langgraph.config import get_stream_writer
from langgraph.types import interrupt
from sqlalchemy import select

from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk
from app.db.models import ChunkMeta

log = logging.getLogger(__name__)


async def expand_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    s = deps.settings
    if not s.SMALL2BIG_ENABLED or not state.reranked or deps.db_sessionmaker is None:
        return {}

    # parent_id -> 名次最高（列表最靠前）的命中块下标；同 parent 只扩一次。
    first_idx_by_parent: dict[str, int] = {}
    for i, c in enumerate(state.reranked):
        pid = str(c.extra.get("parent_section_id") or "").strip()
        if pid and pid not in first_idx_by_parent:
            first_idx_by_parent[pid] = i
    if not first_idx_by_parent:
        return {}

    # 按 rerank 名次序处理各 parent（下标小 = 名次高）。
    parents_in_rank = sorted(first_idx_by_parent.items(), key=lambda kv: kv[1])
    parent_ids = [pid for pid, _ in parents_in_rank]

    siblings = await _fetch_siblings(deps, parent_ids)
    if not siblings:
        return {}

    # 先按预算 / 退化算出每个 parent 要拼哪些 sibling chunk_id（有序），再一次性去 Qdrant
    # 批量取 content，避免 N 次往返。
    plan: list[tuple[int, str, list[str], bool]] = []  # (idx, pid, ordered_ids, degraded)
    for pid, idx in parents_in_rank:
        rows = siblings.get(pid)
        if not rows:
            continue
        hit_chunk_id = state.reranked[idx].chunk_id
        ordered_ids, degraded = _select_sibling_ids(
            rows,
            hit_chunk_id=hit_chunk_id,
            max_section_chars=s.SMALL2BIG_MAX_SECTION_CHARS,
            window=s.SMALL2BIG_NEIGHBOR_WINDOW,
        )
        if ordered_ids:
            plan.append((idx, pid, ordered_ids, degraded))
    if not plan:
        return {}

    all_ids = list({cid for _, _, ids, _ in plan for cid in ids})
    content_map = await deps.dense.fetch_content_by_ids(all_ids)
    if not content_map:
        return {}

    # 名次序累计全局字符预算：超出后靠后的块不再扩；跨界的块截断后收尾。
    budget = max(0, s.SMALL2BIG_TOTAL_BUDGET_CHARS)
    used = 0
    expanded_by_idx: dict[int, tuple[str, bool]] = {}
    for idx, _pid, ordered_ids, degraded in plan:
        text = "\n\n".join(content_map[cid] for cid in ordered_ids if content_map.get(cid))
        if not text:
            continue
        remaining = budget - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
            expanded_by_idx[idx] = (text, degraded)
            used += len(text)
            break
        expanded_by_idx[idx] = (text, degraded)
        used += len(text)

    if not expanded_by_idx:
        return {}

    new_reranked: list[StateChunk] = []
    expanded_payload: list[tuple[StateChunk, bool]] = []
    for i, c in enumerate(state.reranked):
        if i in expanded_by_idx:
            text, degraded = expanded_by_idx[i]
            nc = c.model_copy(update={"expanded_content": text})
            new_reranked.append(nc)
            expanded_payload.append((nc, degraded))
        else:
            new_reranked.append(c)

    await _emit_chunks_expanded(expanded_payload)
    return {"reranked": new_reranked}


async def _fetch_siblings(deps: AgentDeps, parent_ids: list[str]) -> dict[str, list[ChunkMeta]]:
    """按 parent_section_id 批量拉兄弟块元数据（当前 provider），按 document_order 分组有序。

    `chunks_meta` 每 (chunk_id, provider) 一行；按当前 EMBEDDING_PROVIDER 过滤保证每个
    sibling 只一行。失败返回 {} 让上层降级。
    """
    if deps.db_sessionmaker is None or not parent_ids:
        return {}
    provider = deps.settings.EMBEDDING_PROVIDER
    try:
        async with deps.db_sessionmaker() as session:
            stmt = (
                select(ChunkMeta)
                .where(
                    ChunkMeta.parent_section_id.in_(parent_ids),
                    ChunkMeta.provider == provider,
                )
                .order_by(ChunkMeta.document_order.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
    except Exception as exc:
        log.warning("expand_node sibling query failed: %s", exc)
        return {}

    grouped: dict[str, list[ChunkMeta]] = {}
    for r in rows:
        grouped.setdefault(r.parent_section_id, []).append(r)
    for lst in grouped.values():
        lst.sort(key=lambda r: r.document_order)
    return grouped


def _select_sibling_ids(
    rows: list[ChunkMeta],
    *,
    hit_chunk_id: str,
    max_section_chars: int,
    window: int,
) -> tuple[list[str], bool]:
    """决定该 parent 要拼哪些 sibling chunk_id（有序）+ 是否退化。

    整段 `parent_section_chars` 超阈值 → 退化为命中块 document_order 前后各 `window` 块；
    否则取整段全部兄弟块。返回 (ordered_chunk_ids, degraded)。
    """
    section_chars = max((r.parent_section_chars or 0) for r in rows)
    if max_section_chars > 0 and section_chars > max_section_chars:
        hit_pos = next((i for i, r in enumerate(rows) if r.chunk_id == hit_chunk_id), 0)
        lo = max(0, hit_pos - window)
        hi = min(len(rows), hit_pos + window + 1)
        return [r.chunk_id for r in rows[lo:hi]], True
    return [r.chunk_id for r in rows], False


async def _emit_chunks_expanded(chunks: list[tuple[StateChunk, bool]]) -> None:
    """双通道 emit `chunks_expanded`（与 retrieve/rerank 的 chunks_hit/chunks_rerank 同模式）。

    入参每项 = (被扩块, 是否退化窗口)。payload 每条 chunk：
    `chunk_id / spec_id / section_path / section_title / content`（content = 扩段后整段
    文本）+ `degraded`。eval runner 优先用它拼真实上下文（ragas_context_recall /
    faithfulness），前端暂优雅忽略。
    """
    if not chunks:
        return
    payload = [
        {
            "chunk_id": c.chunk_id,
            "spec_id": c.spec_id,
            "section_path": ".".join(c.section_path),
            "section_title": c.section_title,
            "content": c.expanded_content or c.content or "",
            "degraded": degraded,
        }
        for c, degraded in chunks
    ]
    event = {"type": "chunks_expanded", "chunks": payload}

    with contextlib.suppress(RuntimeError):
        writer = get_stream_writer()
        writer(event)

    with contextlib.suppress(RuntimeError):
        await adispatch_custom_event("chunks_expanded", event)
