"""TOC 工具：查 `chunks_meta` 列出指定 spec/section 下的子节。

口径见 `docs/03-development/03-agent.md §4.9 toc`。

接口：
    async def toc_tool(state, *, deps) -> dict[str, Any]

返回结构（写入 `state.tool_results["toc"]`）：
    {
        "spec_id": str | None,
        "section_prefix": list[str],   # 解析出的章节前缀，如 ["5","3"]
        "items": [
            {"section_path", "section_title", "chunk_id", "chunk_type"},
            ...
        ],
        "warning": str | None,
    }

匹配策略：从 user_input / rewritten_queries 抽取 spec_id（形如 `38.331`）与
section 编号（形如 `§5.3`/`5.3.5`）；用 `chunks_meta.spec_id + clause LIKE prefix%`
查询子节（dedup by section_path）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select

from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.db.models import ChunkMeta

log = logging.getLogger(__name__)

_SPEC_RE = re.compile(r"\b(2[1-9]|3[0-8])\.\d{3}\b")
# section 形如 "§5.3" / "section 5.3.5" / "clause 5.3"；单字 "5" 默认不接受，必须伴
# § 或 section/clause 关键词，避免把噪音数字误判成章节
_SECTION_WITH_HINT_RE = re.compile(
    r"(?:§|sec(?:tion)?\.?\s+|clause\s+)(\d+(?:\.\d+){0,4})", re.IGNORECASE
)
_SECTION_BARE_RE = re.compile(r"\b(\d+(?:\.\d+){1,4})\b")  # 至少 "5.3" 形态
_MAX_ITEMS = 60


def _parse_target(user_input: str, rewritten: list[str]) -> tuple[str | None, list[str]]:
    texts = [user_input or "", *rewritten]
    spec_id: str | None = None
    section_prefix: list[str] = []
    for t in texts:
        if not t:
            continue
        if spec_id is None:
            m = _SPEC_RE.search(t)
            if m:
                spec_id = m.group(0)
        if not section_prefix:
            # 先把 spec_id 子串剔掉，避免 "38.331" 被当成章节 38.331
            cleaned = t.replace(spec_id, "") if spec_id else t
            mh = _SECTION_WITH_HINT_RE.search(cleaned)
            if mh:
                section_prefix = [p for p in mh.group(1).split(".") if p]
            else:
                mb = _SECTION_BARE_RE.search(cleaned)
                if mb:
                    section_prefix = [p for p in mb.group(1).split(".") if p]
        if spec_id and section_prefix:
            break
    return spec_id, section_prefix


async def toc_tool(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if deps.db_sessionmaker is None:
        log.info("toc_tool: db_sessionmaker missing")
        return {
            "spec_id": None,
            "section_prefix": [],
            "items": [],
            "warning": "db_sessionmaker unavailable",
        }

    spec_id, section_prefix = _parse_target(state.user_input or "", list(state.rewritten_queries))
    if not spec_id:
        return {
            "spec_id": None,
            "section_prefix": section_prefix,
            "items": [],
            "warning": "no spec_id detected",
        }

    clause_prefix = ".".join(section_prefix) if section_prefix else None
    sm = deps.db_sessionmaker
    try:
        async with sm() as session:
            stmt = select(ChunkMeta).where(ChunkMeta.spec_id == spec_id)
            if clause_prefix:
                stmt = stmt.where(ChunkMeta.clause.like(f"{clause_prefix}%"))
            stmt = stmt.order_by(ChunkMeta.document_order).limit(_MAX_ITEMS * 4)
            rows = (await session.execute(stmt)).scalars().all()
    except Exception as exc:
        log.warning("toc_tool db query failed: %s", exc)
        return {
            "spec_id": spec_id,
            "section_prefix": section_prefix,
            "items": [],
            "warning": f"db error: {exc}",
        }

    seen_section: set[tuple[str, ...]] = set()
    items: list[dict[str, Any]] = []
    for row in rows:
        path = tuple(row.section_path or [])
        if path in seen_section:
            continue
        seen_section.add(path)
        items.append(
            {
                "section_path": list(path),
                "section_title": row.section_title,
                "chunk_id": row.chunk_id,
                "chunk_type": row.chunk_type,
            }
        )
        if len(items) >= _MAX_ITEMS:
            break
    return {
        "spec_id": spec_id,
        "section_prefix": section_prefix,
        "items": items,
        "warning": None,
    }
