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


# Citation 正则（v5：放宽到 LLM 实际输出，让"格式漂移"的 citation 也能被认出来 +
# 软对齐到 chunk）。识别两种形态：
#   1) `[38.331 §5.3]` / `[23.501 §6.3.1]` / `[38.331 §5.3.5.1.2]` — 严格 dotted clause
#   2) `[38.331 §*ControlResourceSet* information element]` / `[38.331 § —]` — 含
#      `*` / 空格 / 破折号占位等非法 section（LLM 抄 chunk header 的常见模式）
#   3) `[38.331]` — 无 § 段（v5 prompt 在 chunk 无 clause 时要求的形态）
# `sect` 段一律放宽到"非 `]`、`¶` 的任意串"，与前端 `CitationInlineSyntax` 对齐；
# `_match_chunk` 负责 strict → fuzzy（title 包含匹配） → spec-only 三段 fallback。
_CITE_RE = re.compile(
    r"\[\s*(?P<spec>[0-9]{2}\.[0-9]{3,4}[A-Za-z]?)" r"(?:\s*§\s*(?P<sect>[^\]¶]+?))?" r"\s*\]"
)

# IE 名 / 章节标题做模糊匹配前的归一：去 markdown 强调符（`*`/`_`/`**`/`__`）、
# 多余空白、`<b>` / `</b>` 等 HTML 残留。保留字母数字和 `-` / `.`。
_EMPHASIS_NORMALIZE_RE = re.compile(r"[\*_]")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")


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
    """从答案文本抽 `[spec §section]`（或仅 `[spec]`）并与 chunks 软对齐。

    对齐规则（`_match_chunk` 三段 fallback）：
    1. **strict**：spec + section_path 前缀双向匹配（LLM 可能写到 5.3 而 chunk 是
       5.3.5.1，或反之）
    2. **fuzzy**：section 段含 `*` / 空格 / IE 名等非 dotted-clause 形态时，
       归一化后与 `chunk.section_title` 做包含匹配（用于救 LLM 抄 chunk header 的
       `[38.331 §*ControlResourceSet* information element]` 这种 in-context 漂移）
    3. **spec-only**：彻底匹不到时退到同 spec 第一条（兼容 `[38.331]` 无 § 形态 +
       任何其它格式漂移）

    同一 (spec, sect) 在同一答案里只保留第一次（保留位置以利前端高亮）。返回结构：
    `[{"chunk_id":..., "spec_id":..., "section_path": "5.3.5", "rank": idx}]`
    """
    if not answer or not chunks:
        return []

    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for m in _CITE_RE.finditer(answer):
        spec = m.group("spec").strip()
        sect_raw = m.group("sect")
        sect = (sect_raw or "").strip().rstrip(".")
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


def _normalize_for_fuzzy(s: str) -> str:
    """归一化 IE 名 / section title 用于模糊匹配：去 HTML 标签、强调符、压空白、小写。"""
    if not s:
        return ""
    s = _HTML_TAG_RE.sub("", s)
    s = _EMPHASIS_NORMALIZE_RE.sub("", s)
    return " ".join(s.split()).lower()


def _looks_like_dotted_clause(sect: str) -> bool:
    """判断 sect 是不是 `5.3.5.1` / `A.1.2` / `5a` 这种合法 clause 编号。

    用于决定走 strict 还是 fuzzy 路径。`-` 也允许（出现在 `36.523-1` 这种 spec 后缀
    场景，但章节侧偶尔也有 `5.3-1` 这种）。
    """
    if not sect:
        return False
    # 允许：字母前缀（Annex）+ 数字 + `.` / `-`；其它字符（`*` / 空格 / `_`）即非法
    return bool(re.fullmatch(r"[A-Za-z]?[\d][\w.\-]*", sect))


def _match_chunk(spec: str, sect: str, chunks: list[StateChunk]) -> StateChunk | None:
    # 1) strict：spec + section_path 前缀双向匹配
    if sect and _looks_like_dotted_clause(sect):
        sect_norm = sect.rstrip(".")
        for c in chunks:
            if c.spec_id != spec:
                continue
            chunk_sect = ".".join(c.section_path)
            if not chunk_sect:
                continue
            if chunk_sect == sect_norm:
                return c
            if chunk_sect.startswith(sect_norm + ".") or sect_norm.startswith(chunk_sect + "."):
                return c

    # 2) fuzzy：sect 不像 dotted clause（含 `*` / 空格 / IE 名等） → 与
    #    chunk.section_title 归一化后包含匹配。让"LLM 抄 chunk header"的 citation
    #    也能拿到对应 chunk_id，前端 chip 才有 chunk 上下文可展示。
    if sect:
        sect_fuzzy = _normalize_for_fuzzy(sect)
        if sect_fuzzy:
            for c in chunks:
                if c.spec_id != spec:
                    continue
                title_fuzzy = _normalize_for_fuzzy(c.section_title)
                if title_fuzzy and (sect_fuzzy in title_fuzzy or title_fuzzy in sect_fuzzy):
                    return c

    # 3) spec-only：同一 spec 任意 chunk（兼容 `[38.331]` 无 § + 彻底漂移兜底）
    for c in chunks:
        if c.spec_id == spec:
            return c
    return None


# tool 路径 preview / definition / snippet 渲染前的 sanitize（v5）。
# 修问题：raw 3GPP markdown 里含 `<b>...</b>` / `*xxx*` 强调符 / 表格管道分隔行；
# 240 字符 preview 又常硬切在表格中间 → 前端 markdown 渲染时 HTML 标签原样回显、
# 管道符乱码、强调符冗余。tool 路径直接把 chunk content/preview 拼成 markdown
# 不过 LLM，所以这层 sanitize 是唯一防线。
_TABLE_DELIM_LINE_RE = re.compile(r"^\s*\|?[\s\-:|]+\|[\s\-:|]+\s*$", re.MULTILINE)
_EMPHASIS_INLINE_RE = re.compile(r"\*{1,3}([^*\n]{1,200}?)\*{1,3}")
# chunker `_section_header` 在 chunk content 头部注入的 `[spec § clause title]`
# 行（独占一行，紧跟两个换行）。preview 是 chunk content 前 N 字符 → 这一行总是
# 在最开头。**必须整行剥掉**，否则前端 `CitationInlineSyntax` 会把它当 citation
# 渲染成 chip——但 tool 路径不走 LLM → message.citations 为空 → chip 永远拿不到
# chunkId → 退化为"未关联 chunk"（用户 2026-05-28 复测复现）。
# 容忍 `*xxx*` / 空格 / `—` 等任意非 `]` 字符（要在 emphasis 解包之前剥，因为
# 解包后 `[spec § PUCCH-Config IE]` 仍是合法 citation 形态会被前端识别）。
_CHUNKER_HEADER_LINE_RE = re.compile(
    r"^\s*\[\s*\d+\.\d+(?:-\d+)?(?:\s*§[^\]\n]*)?\s*\]\s*$",
    re.MULTILINE,
)


def _sanitize_preview(text: str, *, max_chars: int = 180) -> str:
    """tool 路径展示 preview / definition / snippet 前的清洗。

    操作（按序）：
    1. 去 `<b>` / `</i>` 等 HTML 标签（保留内部文本）
    2. **剥 chunker 注入的 `[spec § ...]` 行**（独占一行；避免前端误渲染为 chip）
    3. 去表格分隔行 `|---|---|` / `|:--|`（其它表格行保留但管道符会被压成 ` | `）
    4. 把 `*xxx*` / `**xxx**` 强调符解包成纯文本 `xxx`
    5. 多个换行合并成单空格
    6. 管道符两侧规范化为单空格
    7. 连续空白压成 1 个
    8. 超长截尾加 `…`
    """
    if not text:
        return ""
    s = _HTML_TAG_RE.sub("", text)
    s = _CHUNKER_HEADER_LINE_RE.sub("", s)
    s = _TABLE_DELIM_LINE_RE.sub("", s)
    s = _EMPHASIS_INLINE_RE.sub(r"\1", s)
    s = re.sub(r"\s*\n+\s*", " ", s)
    s = re.sub(r"\s*\|\s*", " | ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" |").strip()
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s


def _render_tool_results(state: AgentState) -> str:
    """tool 路径专用：把 tool_results 渲染成简洁文本（不调 LLM）。

    M4.4 落最小可读形态；M4.5+ 可接入更精细的 prompt 渲染。每段以工具名分组，
    web_search 强制带 §4.9 安全前缀。所有源自 chunk content 的字段（preview /
    glossary.definition / web_search.snippet）都走 `_sanitize_preview` 清掉
    HTML / 强调符 / 表格噪声（v5）。
    """
    results = state.tool_results or {}
    if not results:
        return ""
    parts: list[str] = []
    glossary = results.get("glossary") or {}
    for m in (glossary.get("matches") or [])[:15]:
        parts.append(
            f"- **{m.get('term')}** ({m.get('spec_id')} "
            f"§{'.'.join(m.get('section_path') or [])}): "
            f"{_sanitize_preview(m.get('definition') or '')}"
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
            sect_seg = f"§{sp}" if sp else ""
            preview = _sanitize_preview(h.get("preview") or "")
            parts.append(f"- {h.get('spec_id')} {sect_seg}: {preview}".replace("  ", " "))
    web = results.get("web_search") or {}
    if web.get("results"):
        parts.append(web.get("prefix") or "")
        for r in (web.get("results") or [])[:8]:
            snippet = _sanitize_preview(r.get("snippet") or "")
            parts.append(f"- [{r.get('title')}]({r.get('url')}): {snippet}")
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
