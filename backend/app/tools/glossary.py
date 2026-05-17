"""Glossary 工具：查 PG `glossary` 表，命中后返回短答 + spec 引用。

口径见 `docs/03-development/03-agent.md §4.9 glossary`。

接口：
    async def glossary_tool(state, *, deps) -> dict[str, Any]

返回结构（写入 `state.tool_results["glossary"]`）：
    {
        "matches": [
            {"term", "normalized_term", "definition", "spec_id", "section_path"},
            ...
        ],
        "warning": str | None,
    }

匹配策略：从 user_input / rewritten_queries 抽取候选 term；用 normalized_term 精确匹配
PG `glossary` 表（M4.1 已落 1270 specs / 34k 行）。未命中或 `db_sessionmaker=None`
（测试 / M4.1 未跑）时返回空 matches + warning。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select

from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.db.models import Glossary

log = logging.getLogger(__name__)

_MAX_CANDIDATES = 8
_MAX_RESULTS = 16

_STOPWORDS = {
    "what",
    "is",
    "the",
    "a",
    "an",
    "of",
    "and",
    "or",
    "for",
    "in",
    "on",
    "to",
    "describe",
    "explain",
    "define",
    "definition",
    "abbreviation",
    "list",
    "give",
    "me",
    "please",
    "tell",
    "about",
    "全",
    "称",
    "是什么",
    "缩写",
    "术语",
    "definitions",
}

# 提取大写缩写（≥2 字母）或保留大小写的多字符 token
_TOKEN_RE = re.compile(r"[A-Z][A-Za-z0-9\-]+|[a-z]{3,}")


def _extract_candidates(user_input: str, rewritten: list[str]) -> list[str]:
    """从 user_input + rewritten_queries 抽 candidate term。

    策略：优先取**大写缩写**（AMF / PDU / N1 ...）；不足时退到长 token。
    """
    pool: list[str] = []
    seen: set[str] = set()
    for text in [user_input, *rewritten]:
        if not text:
            continue
        for tok in _TOKEN_RE.findall(text):
            key = tok.upper()
            if key in seen:
                continue
            if tok.lower() in _STOPWORDS:
                continue
            if len(tok) < 2:
                continue
            seen.add(key)
            pool.append(tok)
            if len(pool) >= _MAX_CANDIDATES:
                return pool
    return pool


async def glossary_tool(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if deps.db_sessionmaker is None:
        log.info("glossary_tool: db_sessionmaker missing, returning empty (M4.1 not ready?)")
        return {"matches": [], "warning": "db_sessionmaker unavailable"}

    candidates = _extract_candidates(state.user_input or "", list(state.rewritten_queries))
    if not candidates:
        return {"matches": [], "warning": None}

    # PG `glossary.normalized_term` 由 ingestion 端落库时已经 lower()（M4.1 约定）
    normalized = [c.lower() for c in candidates]
    sm = deps.db_sessionmaker
    matches: list[dict[str, Any]] = []
    try:
        async with sm() as session:
            stmt = (
                select(Glossary).where(Glossary.normalized_term.in_(normalized)).limit(_MAX_RESULTS)
            )
            rows = (await session.execute(stmt)).scalars().all()
    except Exception as exc:
        log.warning("glossary_tool db query failed: %s", exc)
        return {"matches": [], "warning": f"db error: {exc}"}

    for row in rows:
        matches.append(
            {
                "term": row.term,
                "normalized_term": row.normalized_term,
                "definition": row.definition,
                "spec_id": row.spec_id,
                "section_path": list(row.section_path or []),
            }
        )
    return {"matches": matches, "warning": None}
