"""rewrite 节点（M4.3 启用）。

simple fast path 由 classify 直接给出 `rewritten_query`，因此 M4.2 graph 不接入
本节点；这里只为 M4.3 complex 分支预埋实现，并提供统一的 unit 测试入口。
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


async def rewrite_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    user_input = (state.user_input or "").strip()
    if not user_input:
        return {"rewritten_queries": []}

    prompt = render("rewrite", user_input=user_input)
    try:
        # LIGHT 模型 (mimo-v2.5) 是 reasoning model：先在 reasoning_content 消耗 token
        # 再产 content。早期 max_tokens=120 → reasoning 一次吃满，content 永远 ''，
        # rewrite 永远回退到 user_input（complex 链路的改写步骤事实上一直是 no-op）。
        # 1024 留 reasoning ~800 + 改写 ~80 的余量，远超实测峰值（200-300）。
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_LIGHT_MODEL,
            temperature=0.0,
            max_tokens=1024,
        )
    except LLMError as exc:
        log.warning("rewrite_node llm failed, fallback to user_input: %s", exc)
        return {"rewritten_queries": [user_input]}

    try:
        rewritten = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"rewritten_queries": [user_input]}

    if not isinstance(rewritten, str):
        return {"rewritten_queries": [user_input]}
    lines = [ln.strip() for ln in rewritten.splitlines() if ln.strip()]
    cleaned = lines[0].strip('"').strip() if lines else ""
    return {"rewritten_queries": [cleaned or user_input]}
