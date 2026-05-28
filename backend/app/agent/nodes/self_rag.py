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

M4.9 batch B.1（R8 + O3）：citation 真实性核对。
LLM 的 grounding 判定不直接看 `[spec §section]` 引用是否落在 reranked 集合内；
此处对 `state.final_answer` 用 `_CITE_RE` 重抽 (spec_id, section_path)，与
`state.reranked` 做严格前缀交集（无 generate_node 的 "同 spec 任意 chunk" 兜底）：
- hit_rate == 1.0          → 不动
- 0.5 ≤ hit_rate < 1.0     → confidence *= hit_rate（部分 hallucinate，弱化展示）
- hit_rate < 0.5 + retry   → 强制 verdict=retry，未命中 spec/section 加入 missing
- hit_rate < 0.5 + simple  → 保持 accept（M4.2 不死循环口径），但 confidence=0
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

from app.agent.deps import AgentDeps
from app.agent.prompts import render
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk
from app.core.errors import LLMError

log = logging.getLogger(__name__)

# 与 `generate.py` 共用同一正则，保持 (spec, section) 抽取语义一致
_CITE_RE = re.compile(
    r"\[\s*(?P<spec>[0-9]{2}\.[0-9]{3,4}[A-Za-z]?)\s*§\s*(?P<sect>[A-Za-z0-9.\-/]+)\s*\]"
)
_HIT_RATE_THRESHOLD = 0.5


class SelfRagOutput(BaseModel):
    faithful: bool = True
    coverage: float = 0.0
    confidence: float = 0.0
    verdict: Literal["accept", "retry", "insufficient"] = "accept"
    missing_aspects: list[str] = []


def _citation_hit_rate(answer: str, reranked: list[StateChunk]) -> tuple[float, list[str]]:
    """对 answer 里出现的每个 [spec §section]，严格匹配 reranked 集合。

    严格匹配规则（无 generate_node `_match_chunk` 的"同 spec 任意 chunk"兜底）：
    - spec_id 必须完全相同
    - chunk.section_path join('.') 与 cite section 互为前缀（任一方向）

    返回 (hit_rate, missing_specs)：
    - 无 citation → (1.0, [])，视作不需要核对
    - missing_specs 用于在 allow_retry 时塞回 rewritten_queries
    """
    cites: list[tuple[str, str]] = []
    for m in _CITE_RE.finditer(answer or ""):
        spec = m.group("spec").strip()
        sect = m.group("sect").strip().rstrip(".")
        cites.append((spec, sect))
    if not cites:
        return 1.0, []

    hits = 0
    missing: list[str] = []
    for spec, sect in cites:
        matched = False
        for c in reranked:
            if c.spec_id != spec:
                continue
            chunk_sect = ".".join(c.section_path)
            if (
                chunk_sect == sect
                or chunk_sect.startswith(sect + ".")
                or sect.startswith(chunk_sect + ".")
            ):
                matched = True
                break
        if matched:
            hits += 1
        else:
            missing.append(f"{spec} §{sect}")
    return hits / len(cites), missing


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

    # citation 真实性核对（R8 + O3）：先于 simple-path verdict 强制 accept 计算
    hit_rate, missing_cites = _citation_hit_rate(state.final_answer, list(state.reranked))
    if hit_rate < _HIT_RATE_THRESHOLD:
        if allow_retry:
            verdict = "retry"
            for c in missing_cites:
                if c and c not in missing:
                    missing.append(c)
            confidence = 0.0
        else:
            # simple path 不死循环：保留 accept 链路，confidence 归零作为下游信号
            confidence = 0.0
    elif hit_rate < 1.0:
        confidence = confidence * hit_rate

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
