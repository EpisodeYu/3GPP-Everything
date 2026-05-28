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
        # thinking=disabled：rewrite 是短输出（一句改写），不需要 reasoning。
        # mimo 思考模式下 temperature=0 被强制 1.0，同题两次改写出来不一样，
        # complex 链路检索无法复现。disabled 后 temp=0 真生效，可复现且省成本。
        # 没 reasoning 占用，max_tokens 回归小值即可。
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_LIGHT_MODEL,
            temperature=0.0,
            max_tokens=512,
            thinking={"type": "disabled"},
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
