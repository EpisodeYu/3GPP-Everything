"""hyde 节点（M4.3 complex 分支 + 2026-05-31 字符级流式 reasoning）。

口径见 `docs/03-development/03-agent.md §4.3`。让 LLM 假装写一段「理想答案的章节
文本」（200-400 tokens），用其 embedding 一起进检索。**仅 complex 走**。

2026-05-31 起改用 `LiteLLMClient.chat_stream()` 真流式拿 token，并通过
`adispatch_custom_event("node_progress", {"node":"hyde","delta":...})` 推字符级
事件给前端 reasoning 折叠框（口径见 `03-agent.md §7` SSE 表）。流式失败兜底
回非流式 `chat()`，并把整段 hyde_doc 一次性补打一次 progress event，让前端 UI
不至于空白（与 generate 节点 `_stream_answer` 同款 fallback 模式）。

失败处理：LLM 调用全失败时，把 hyde_doc 留空（None），不阻塞 retrieve；retrieve
对 None 已有兜底处理。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from app.agent.deps import AgentDeps
from app.agent.prompts import render
from app.agent.state import AgentState
from app.core.errors import LLMError

log = logging.getLogger(__name__)


async def hyde_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    # 多轮：用 contextualize 消解后的自包含问题（effective_query）写假设答案；首轮回退原文。
    user_input = (state.effective_query or "").strip()
    if not user_input:
        return {"hyde_doc": None}

    prompt = render("hyde", user_input=user_input)
    messages = [{"role": "user", "content": prompt}]

    text = await _stream_hyde(deps, messages)
    if text is None:
        # 流式失败 → 兜底再来一次非流式 chat()，把整段一次性 emit 给前端
        try:
            # AGENT 模型 (mimo-v2.5-pro) 也是 reasoning model：先 reasoning 再 content。
            # 早期 max_tokens=600 — 实测 reasoning ~200-275，剩 300-400 token 给 doc，
            # 简单问题就撞顶被截（finish=length, content 收尾不完整），影响 embedding
            # 质量。HyDE 目标是 200-400 token 的「理想答案章节文本」，HyDE 内容最长且
            # reasoning 方差大，8192 给 peak reasoning ~3000 + doc ~800 留 ~2.5x 余量，
            # 复杂题也不截断；content 截断对 embedding 影响直接，宁可给足上限。
            resp = await deps.llm.chat(
                messages=messages,
                model=deps.settings.LLM_AGENT_MODEL,
                temperature=0.2,
                max_tokens=8192,
            )
        except LLMError as exc:
            log.warning("hyde_node llm fallback failed: %s", exc)
            return {"hyde_doc": None}
        text = _extract_text(resp)
        if text:
            await _emit_progress(text)

    return {"hyde_doc": text or None}


async def _stream_hyde(deps: AgentDeps, messages: list[dict[str, Any]]) -> str | None:
    """流式跑 hyde 并逐 chunk emit `node_progress`；任何异常返回 None 让 caller 兜底。"""
    buf: list[str] = []
    try:
        async for chunk in deps.llm.chat_stream(
            messages=messages,
            model=deps.settings.LLM_AGENT_MODEL,
            temperature=0.2,
            max_tokens=8192,
        ):
            delta = _extract_delta(chunk)
            if not delta:
                continue
            buf.append(delta)
            await _emit_progress(delta)
    except LLMError as exc:
        log.warning("hyde_node stream failed, will fallback: %s", exc)
        return None
    return "".join(buf).strip() or None


async def _emit_progress(delta: str) -> None:
    """通过 LangGraph 双通道 emit `node_progress`，与 retrieve_node 的 chunks_hit emit 同模式。

    任一通道在当前 graph 上下文不可用（单测直接 `await hyde_node(...)`）抛
    RuntimeError，吞掉，不影响主路径。
    """
    event = {"node": "hyde", "delta": delta}
    with contextlib.suppress(RuntimeError):
        writer = get_stream_writer()
        writer({"type": "node_progress", **event})
    with contextlib.suppress(RuntimeError):
        await adispatch_custom_event("node_progress", event)


def _extract_delta(chunk: dict[str, Any]) -> str:
    """OpenAI 兼容 stream chunk → token 增量字符串。"""
    try:
        delta = chunk["choices"][0]["delta"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else ""


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
