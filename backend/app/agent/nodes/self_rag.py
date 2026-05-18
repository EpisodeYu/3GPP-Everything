"""self_rag 节点：grounding + retry loop（M4.2 grounding-only / M4.3 retry loop）。

口径见 `docs/03-development/03-agent.md §4.8`。

行为：
- 调 mimo-v2.5 拿 `SelfRagOutput` JSON
- `allow_retry=False`（M4.2 simple fast path）：无论 verdict 是 retry 还是
  insufficient，都把 verdict 强制为 `accept` 后返回 END（保留原 confidence /
  faithful 给前端展示）
- `allow_retry=True`（M4.3 complex 分支）：verdict=retry 时
  - 把 retry_count + 1
  - 把 missing_aspects 拼到 rewritten_queries 末尾（下一轮 retrieve 用更细的 query）
  - 路由由 graph 的 conditional edge 判定：`retry_count >= 2` 即使 verdict=retry 也走 END
- LLM 失败时退化到 verdict=accept + confidence=0，避免 graph 卡死
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

from app.agent.deps import AgentDeps
from app.agent.prompts import render
from app.agent.state import AgentState
from app.core.errors import LLMError

log = logging.getLogger(__name__)


class SelfRagOutput(BaseModel):
    faithful: bool = True
    coverage: float = 0.0
    confidence: float = 0.0
    verdict: Literal["accept", "retry", "insufficient"] = "accept"
    missing_aspects: list[str] = []


async def self_rag_node(
    state: AgentState,
    *,
    deps: AgentDeps,
    allow_retry: bool = False,
) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    if not state.final_answer or not state.reranked:
        # 没东西可校验 — accept 兜底，置低 confidence
        return {
            "self_rag_verdict": "accept",
            "self_rag_missing": [],
            "confidence": 0.0,
        }

    prompt = render(
        "self_rag",
        chunks=[
            {
                "spec_id": c.spec_id,
                "section_path": list(c.section_path),
                "content": c.content,
            }
            for c in state.reranked
        ],
        answer=state.final_answer,
        user_input=state.user_input,
    )

    try:
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_LIGHT_MODEL,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except LLMError as exc:
        log.warning("self_rag_node llm failed: %s", exc)
        return {
            "self_rag_verdict": "accept",
            "self_rag_missing": [],
            "confidence": 0.0,
        }

    parsed = _parse(resp) or SelfRagOutput()

    verdict: Literal["accept", "retry", "insufficient"] = parsed.verdict
    missing = list(parsed.missing_aspects or [])
    if not allow_retry and verdict != "accept":
        # M4.2 simple path: 不死循环、不升级；保留原诊断信息但强制 accept
        verdict = "accept"

    update: dict[str, Any] = {
        "self_rag_verdict": verdict,
        "self_rag_missing": missing,
        "confidence": float(parsed.confidence),
    }
    if allow_retry and verdict == "retry":
        # 把 missing_aspects 拼到 rewritten_queries 末尾，下一轮 retrieve_node 直接消费
        new_queries: list[str] = list(state.rewritten_queries)
        seen = {q.lower() for q in new_queries}
        for asp in missing:
            asp = (asp or "").strip()
            if asp and asp.lower() not in seen:
                new_queries.append(asp)
                seen.add(asp.lower())
        update["rewritten_queries"] = new_queries
        update["retry_count"] = state.retry_count + 1
    return update


def _parse(resp: dict[str, Any]) -> SelfRagOutput | None:
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        log.warning("self_rag_node json parse failed: %s", content[:200])
        return None
    try:
        return SelfRagOutput.model_validate(data)
    except ValidationError as exc:
        log.warning("self_rag_node schema validation failed: %s", exc)
        return None
