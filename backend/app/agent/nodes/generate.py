"""generate 节点：mimo-v2.5-pro 严格 grounding 生成最终答案。

口径见 `docs/03-development/03-agent.md §4.7`。

实现要点：
- 用 `LiteLLMClient.chat_stream()` 真流式拿 token；每个 chunk 通过
  `adispatch_custom_event("token", {"delta": ...})` 透传给 backend SSE 路由
  （chat.py 的 `on_custom_event` 处理器 → `token` 事件 → 前端 `partialAnswer`
  逐字渲染）。不走 `on_chat_model_stream` 路径：那条路径只在节点里调用
  LangChain 兼容 chat model 时才触发，本项目走自定义 httpx 客户端。
- 用 `parse_citations()` 从答案文本里抽 `[spec_id §section_path]`，与 reranked
  列表按 (spec_id, section_path) 软对齐。
- "未在已索引 3GPP 文档中找到 …" 兜底：reranked 为空直接产 fallback 答案。
- 流式失败兜底（网络抖动 / LiteLLM 异常）：catch `LLMError` 后回退到非流式
  `chat()` 再试一次；都失败才返回 fallback。
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
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    chunks = state.reranked
    if not chunks:
        # tool 路径：没有 reranked chunks 但 tool_dispatch 写了 tool_results，
        # 直接把工具结果渲染成结构化短答，不走 LLM（保持成本可预测；同时为
        # M4.4 验收"工具结果能到达 final_answer"留口子）
        tool_text = _render_tool_results(state)
        if tool_text:
            return {
                "final_answer": tool_text,
                "citations": [],
                "confidence": 0.5,
            }
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
    messages = [{"role": "user", "content": prompt}]

    answer = await _stream_answer(deps, messages)
    if answer is None:
        # 流式失败 → 兜底再来一次非流式 chat()
        try:
            resp = await deps.llm.chat(
                messages=messages,
                model=deps.settings.LLM_AGENT_MODEL,
                temperature=0.1,
            )
        except LLMError as exc:
            log.warning("generate_node llm fallback failed: %s", exc)
            msg = _FALLBACK_ZH if state.user_language == "zh" else _FALLBACK_EN
            return {"final_answer": msg, "citations": [], "confidence": 0.0}
        answer = _extract_text(resp)
        # 兜底路径也补一次 token event，让前端的 partialAnswer 一次性显示出来
        if answer:
            await _emit_token(answer)

    citations = parse_citations(answer, chunks)
    return {
        "final_answer": answer,
        "citations": citations,
    }


async def _stream_answer(deps: AgentDeps, messages: list[dict[str, Any]]) -> str | None:
    """流式跑 LLM 并逐 chunk emit token 事件；任何异常返回 None 让 caller 兜底。"""
    buf: list[str] = []
    try:
        async for chunk in deps.llm.chat_stream(
            messages=messages,
            model=deps.settings.LLM_AGENT_MODEL,
            temperature=0.1,
        ):
            delta = _extract_delta(chunk)
            if not delta:
                continue
            buf.append(delta)
            await _emit_token(delta)
    except LLMError as exc:
        log.warning("generate_node stream failed, will fallback: %s", exc)
        return None
    return "".join(buf).strip()


def _extract_delta(chunk: dict[str, Any]) -> str:
    """OpenAI 兼容 stream chunk → token 增量字符串。"""
    try:
        delta = chunk["choices"][0]["delta"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else ""


async def _emit_token(delta: str) -> None:
    """通过 LangGraph 两条流通道 emit `token` 事件。

    与 retrieve / rerank 节点 emit `chunks_hit` 同一双轨模式（口径
    `docs/03-development/03-agent.md §7`）：
    - `get_stream_writer()` → `astream(stream_mode="custom")`
    - `adispatch_custom_event` → `astream_events(v=v2)` 的 `on_custom_event`

    单测直接 `await generate_node(...)`（无 LangGraph 上下文）时会抛
    RuntimeError，吞掉，不影响主路径。
    """
    if not delta:
        return
    event = {"delta": delta}
    with contextlib.suppress(RuntimeError):
        writer = get_stream_writer()
        writer({"type": "token", **event})
    with contextlib.suppress(RuntimeError):
        await adispatch_custom_event("token", event)


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


def _render_tool_results(state: AgentState) -> str:
    """tool 路径专用：把 tool_results 渲染成简洁文本（不调 LLM）。

    M4.4 落最小可读形态；M4.5+ 可接入更精细的 prompt 渲染。每段以工具名分组，
    web_search 强制带 §4.9 安全前缀。
    """
    results = state.tool_results or {}
    if not results:
        return ""
    parts: list[str] = []
    glossary = results.get("glossary") or {}
    for m in (glossary.get("matches") or [])[:15]:
        parts.append(
            f"- **{m.get('term')}** ({m.get('spec_id')} "
            f"§{'.'.join(m.get('section_path') or [])}): {m.get('definition')}"
        )
    toc = results.get("toc") or {}
    if toc.get("items"):
        prefix = ".".join(toc.get("section_prefix") or [])
        header = f"### {toc.get('spec_id')} §{prefix}" if prefix else f"### {toc.get('spec_id')}"
        parts.append(header)
        for it in (toc.get("items") or [])[:60]:
            sp = ".".join(it.get("section_path") or [])
            parts.append(f"- §{sp} {it.get('section_title')}")
    params = results.get("params") or {}
    if params.get("hits"):
        parts.append("### Parameter / IE hits")
        for h in (params.get("hits") or [])[:20]:
            sp = ".".join(h.get("section_path") or [])
            parts.append(f"- {h.get('spec_id')} §{sp}: {h.get('preview')}")
    web = results.get("web_search") or {}
    if web.get("results"):
        parts.append(web.get("prefix") or "")
        for r in (web.get("results") or [])[:8]:
            parts.append(f"- [{r.get('title')}]({r.get('url')}): {r.get('snippet')}")
    return "\n".join(p for p in parts if p).strip()


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
