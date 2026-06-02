"""contextualize 节点：多轮指代消解（2026-06-02 接通真多轮，A 层）。

口径见 `docs/03-development/03-agent.md §6.1`。

职责：把依赖上文的追问（"它的默认值？"/"那 5G 呢？"/"继续展开第 2 点"）用对话历史
补全成**自包含问题**，写入 `state.contextualized_input`；后续 classify / rewrite /
hyde / retrieve / self_rag 经 `AgentState.effective_query` 统一消费它，从而让检索拿到
被消解后的查询。generate 仍回答原始 `user_input`（历史另作只读上下文，见 generate 节点）。

只在 `state.history` 非空时由 graph 条件入边触发（首轮直连 classify，零额外 LLM 调用）；
本节点内部对空 history / 空输入再做一层防御性短路，便于被单测或其它图直接调用。

模型：`LLM_LIGHT_MODEL` + `thinking=disabled`（短输出、需可复现，理由同 rewrite/classify）。
失败兜底：LLM / 解析任一失败 → 回退到原始 `user_input`，绝不阻塞主链路。
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


async def contextualize_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    user_input = (state.user_input or "").strip()
    if not user_input:
        return {"contextualized_input": ""}

    # 无历史 → 当前问题即自包含，不调 LLM。
    if not state.history:
        return {"contextualized_input": user_input}

    prompt = render("contextualize", history=state.history, user_input=user_input)
    try:
        # thinking=disabled：指代消解是短改写，不需 reasoning；mimo 思考模式下 temp=0
        # 被强制 1.0 → 同一追问消解结果漂移，检索不可复现。disabled 后 temp=0 真生效。
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_LIGHT_MODEL,
            temperature=0.0,
            max_tokens=256,
            thinking={"type": "disabled"},
        )
    except LLMError as exc:
        log.warning("contextualize_node llm failed, fallback to user_input: %s", exc)
        return {"contextualized_input": user_input}

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"contextualized_input": user_input}

    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return {"contextualized_input": user_input}

    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    cleaned = lines[0].strip('"').strip() if lines else ""
    return {"contextualized_input": cleaned or user_input}
