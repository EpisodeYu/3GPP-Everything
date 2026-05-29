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

**v6（citation 索引方案）退役 citation 真实性核对**：
M4.9 batch B.1 引入的 `_citation_hit_rate` 在 v2-v5 `[spec §section]` 文本引用
时代有意义——LLM 会编不存在的 section/IE 名，hit_rate < 0.5 触发 retry / 弱化
confidence。v6 切到 `[N]` 索引后：
- LLM 不再拼 spec/section，只输出索引 N；
- generate_node `parse_citations` 已对越界 N 做 drop（不会落到 self_rag）；
- 所有进入 self_rag 的 citation 必然指向 reranked 集合内的 chunk，hit_rate 恒为 1.0。

因此 v6 删除 `_citation_hit_rate` 整段逻辑，grounding 真实性全权交给
LLM `faithful` + `coverage` 字段判定（mimo `thinking=disabled` 后 verdict 稳定）。
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
        # thinking=disabled：self_rag 输出固定 schema 的 verdict JSON，无需 reasoning；
        # 思考模式下 temp=0 被强制 1.0 → 同样事实/回答会偶发返不同 verdict（accept ↔
        # retry 跳变），retry 路径不稳定。disabled 后 verdict 完全确定，retry 行为
        # 可复现。
        # 不传 response_format：mimo 官方文档 `type` 字段仅支持 `text`，prompt 已
        # "Emit ONE JSON object — no prose, no markdown fence" 强约束。
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_LIGHT_MODEL,
            temperature=0.0,
            thinking={"type": "disabled"},
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
    confidence = float(parsed.confidence)

    # v6 索引方案下 citation 已无漂移空间（parse_citations 已对越界 N 做 drop），
    # 不再做 _citation_hit_rate 真实性核对——LLM faithful/coverage 字段是唯一
    # grounding 信号。

    if not allow_retry and verdict != "accept":
        # M4.2 simple path: 不死循环、不升级；保留原诊断信息但强制 accept
        verdict = "accept"

    update: dict[str, Any] = {
        "self_rag_verdict": verdict,
        "self_rag_missing": missing,
        "confidence": confidence,
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
