"""classify 节点：路由 / 复杂度 / fast-path 改写一次过。

口径见 `docs/03-development/03-agent.md §4.1`。
模型：`mimo-v2.5`（轻量），结构化输出 `ClassifyOutput` JSON。

节点边界检测（cancel/pause）放在每个节点开头；M4.2 simple 路径不走 pause，但保留
统一断言以便 M4.5 接入。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, cast

from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

from app.agent.deps import AgentDeps
from app.agent.prompts import render
from app.agent.state import AgentState
from app.core.errors import LLMError

log = logging.getLogger(__name__)


class ClassifyOutput(BaseModel):
    """与 prompts/classify.md 顶部 JSON schema 一一对应。"""

    query_class: Literal["definition", "procedure", "tool", "unknown"]
    complexity: Literal["simple", "complex"]
    detected_language: Literal["zh", "en", "mixed"]
    rewritten_query: str
    needs_explicit_tools: list[str] = []
    reason: str = ""


_FALLBACK = ClassifyOutput(
    query_class="unknown",
    complexity="simple",
    detected_language="en",
    rewritten_query="",
    needs_explicit_tools=[],
    reason="classify_fallback",
)


async def classify_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    user_input = (state.user_input or "").strip()
    if not user_input:
        # 没有问题就别浪费 LLM 调用；后续 retrieve/generate 会被 graph 路由跳过
        return {
            "query_class": "unknown",
            "complexity": "simple",
            "rewritten_queries": [],
            "user_language": state.user_language,
        }

    prompt = render("classify", user_input=user_input)
    try:
        # thinking=disabled：classify 输出是固定 schema 的 JSON，不需要 reasoning；
        # mimo 思考模式下 temperature=0 被强制改成 1.0，无法保证同输入同分类（同
        # 一题在 simple/complex 间跳变会让路由不稳）。disabled 后 temp=0 真生效，
        # reasoning_tokens=0，分类完全确定性。
        # 不传 response_format：mimo 官方文档 `type` 字段仅支持 `text`，json_object
        # 是 LiteLLM 透传 + mimo 静默忽略；prompt 已 "ONLY the JSON object" 强约束。
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_LIGHT_MODEL,
            temperature=0.0,
            thinking={"type": "disabled"},
        )
    except LLMError as exc:
        log.warning("classify_node llm failed, using fallback: %s", exc)
        out = _FALLBACK
    else:
        out = _parse_classify(resp) or _FALLBACK

    rewritten = out.rewritten_query.strip() or user_input
    explicit_tools = list(out.needs_explicit_tools or [])
    explicit_tools.extend(t for t in state.explicit_tools if t not in explicit_tools)

    detected_lang = "zh" if out.detected_language == "zh" else "en"

    return {
        "query_class": out.query_class,
        "complexity": out.complexity,
        "user_language": cast(Any, detected_lang),
        "rewritten_queries": [rewritten],
        "explicit_tools": explicit_tools,
    }


def _parse_classify(resp: dict[str, Any]) -> ClassifyOutput | None:
    """OpenAI 兼容 chat completion → ClassifyOutput。"""
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("classify_node response shape unexpected: %s", exc)
        return None
    if isinstance(content, list):
        # response_format=json_object 在某些 LiteLLM provider 里会回 array of parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        log.warning("classify_node json parse failed: %s", content[:200])
        return None
    try:
        return ClassifyOutput.model_validate(data)
    except ValidationError as exc:
        log.warning("classify_node schema validation failed: %s", exc)
        return None
