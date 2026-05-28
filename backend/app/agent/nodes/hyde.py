"""hyde 节点（M4.3 complex 分支）。

口径见 `docs/03-development/03-agent.md §4.3`。让 LLM 假装写一段"理想答案的章节
文本"（200-400 tokens），用其 embedding 一起进检索。**仅 complex 走**。

失败处理：LLM 调用失败时，把 hyde_doc 留空（None），不阻塞 retrieve；retrieve
对 None 已有兜底处理。
"""

from __future__ import annotations

import logging
from typing import Any

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

    user_input = (state.user_input or "").strip()
    if not user_input:
        return {"hyde_doc": None}

    prompt = render("hyde", user_input=user_input)
    try:
        # AGENT 模型 (mimo-v2.5-pro) 也是 reasoning model：先 reasoning 再 content。
        # 早期 max_tokens=600 — 实测 reasoning ~200-275，剩 300-400 token 给 doc，
        # 简单问题就撞顶被截（finish=length, content 收尾不完整），影响 embedding
        # 质量。HyDE 目标是 200-400 token 的"理想答案章节文本"，HyDE 内容最长且
        # reasoning 方差大，8192 给 peak reasoning ~3000 + doc ~800 留 ~2.5x 余量，
        # 复杂题也不截断；content 截断对 embedding 影响直接，宁可给足上限。
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_AGENT_MODEL,
            temperature=0.2,
            max_tokens=8192,
        )
    except LLMError as exc:
        log.warning("hyde_node llm failed: %s", exc)
        return {"hyde_doc": None}

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"hyde_doc": None}
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return {"hyde_doc": None}
    text = content.strip()
    return {"hyde_doc": text or None}
