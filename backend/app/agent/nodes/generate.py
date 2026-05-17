"""generate 节点：mimo-v2.5-pro 严格 grounding 生成最终答案。

口径见 `docs/03-development/03-agent.md §4.7`。

M4.2 实现要点：
- 用 `LiteLLMClient.chat()` 非流式拿全文（SSE 流式由 backend M4.3 在 graph.astream_events
  外层重新映射；此处保持节点纯 async 函数返回完整 final_answer）
- 用 `parse_citations()` 从答案文本里抽 `[spec_id §section_path]`，与 reranked 列表
  按 (spec_id, section_path) 软对齐
- "未在已索引 3GPP 文档中找到 …" 兜底：reranked 为空直接产 fallback 答案
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langgraph.errors import NodeInterrupt

from app.agent.deps import AgentDeps
from app.agent.prompts import render
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk
from app.core.errors import LLMError

log = logging.getLogger(__name__)


# `[38.331 §5.3]` / `[23.501 §6.3.1]` / `[38.331 §5.3.5.1.2]`
_CITE_RE = re.compile(
    r"\[\s*(?P<spec>[0-9]{2}\.[0-9]{3,4}[A-Za-z]?)\s*§\s*(?P<sect>[A-Za-z0-9.\-/]+)\s*\]"
)


_FALLBACK_EN = "Not found in the indexed 3GPP documents."
_FALLBACK_ZH = "未在已索引 3GPP 文档中找到与该问题直接相关的内容。"


async def generate_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        raise NodeInterrupt("cancelled by user")
    if state.paused:
        raise NodeInterrupt("paused by user")

    chunks = state.reranked
    if not chunks:
        msg = _FALLBACK_ZH if state.user_language == "zh" else _FALLBACK_EN
        return {
            "final_answer": msg,
            "citations": [],
            "confidence": 0.0,
        }

    prompt = render(
        "generate_qa",
        chunks=[_chunk_view(c) for c in chunks],
        user_input=state.user_input,
        user_language=state.user_language,
    )

    try:
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_AGENT_MODEL,
            temperature=0.1,
        )
    except LLMError as exc:
        log.warning("generate_node llm failed: %s", exc)
        msg = _FALLBACK_ZH if state.user_language == "zh" else _FALLBACK_EN
        return {"final_answer": msg, "citations": [], "confidence": 0.0}

    answer = _extract_text(resp)
    citations = parse_citations(answer, chunks)
    return {
        "final_answer": answer,
        "citations": citations,
    }


def parse_citations(answer: str, chunks: list[StateChunk]) -> list[dict[str, Any]]:
    """从答案文本抽 `[spec §section]` 并与 chunks 软对齐。

    对齐规则：
    1. spec_id 必须完全相同
    2. section_path 取 chunk.section_path join('.')，**前缀** 匹配命中即视作对应
       chunk（LLM 可能写到 5.3 而 chunk 是 5.3.5.1）
    3. 同一 (spec, section) 多次出现只保留第一次（保留位置以利前端高亮）
    返回结构：`[{"chunk_id":..., "spec_id":..., "section_path": "5.3.5", "rank": idx}]`
    """
    if not answer or not chunks:
        return []

    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for m in _CITE_RE.finditer(answer):
        spec = m.group("spec").strip()
        sect = m.group("sect").strip().rstrip(".")
        key = (spec, sect)
        if key in seen:
            continue
        match_chunk = _match_chunk(spec, sect, chunks)
        if match_chunk is None:
            continue
        seen.add(key)
        out.append(
            {
                "chunk_id": match_chunk.chunk_id,
                "spec_id": match_chunk.spec_id,
                "section_path": ".".join(match_chunk.section_path),
                "section_title": match_chunk.section_title,
                "cite_section_path": sect,
                "rerank_score": match_chunk.score_rerank,
            }
        )
    return out


def _match_chunk(spec: str, sect: str, chunks: list[StateChunk]) -> StateChunk | None:
    sect_norm = sect.rstrip(".")
    for c in chunks:
        if c.spec_id != spec:
            continue
        chunk_sect = ".".join(c.section_path)
        if chunk_sect == sect_norm:
            return c
        if chunk_sect.startswith(sect_norm + ".") or sect_norm.startswith(chunk_sect + "."):
            return c
    # 退而求其次：同一 spec 任意 chunk
    for c in chunks:
        if c.spec_id == spec:
            return c
    return None


def _chunk_view(c: StateChunk) -> dict[str, Any]:
    return {
        "chunk_id": c.chunk_id,
        "spec_id": c.spec_id,
        "section_path": list(c.section_path),
        "section_title": c.section_title,
        "content": c.content,
    }


def _extract_text(resp: dict[str, Any]) -> str:
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return ""
    return content.strip()
