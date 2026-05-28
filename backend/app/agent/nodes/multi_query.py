"""multi_query 节点（M4.3 complex 分支）。

口径见 `docs/03-development/03-agent.md §4.4`。把改写后的 query 拆 3-5 个不同角度
sub-query，统一塞到 `state.rewritten_queries` 给 retrieve_node 消费。

输入：`state.rewritten_queries[0]`（由 rewrite_node 或 classify 产出）
输出：`state.rewritten_queries = [primary, sub1, sub2, ...]`（primary 保留以
保障 retrieve 至少能跑原 query；sub_query 拼在后面，retrieve 内部去重 + RRF 融合）

失败处理：LLM 失败或解析失败 → 保留原 rewritten_queries 不动。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langgraph.types import interrupt

from app.agent.deps import AgentDeps
from app.agent.prompts import render
from app.agent.state import AgentState
from app.core.errors import LLMError

log = logging.getLogger(__name__)

_MAX_SUB_QUERIES = 5


async def multi_query_node(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    if state.cancelled:
        interrupt({"reason": "cancelled by user"})
    if state.paused:
        interrupt({"reason": "paused by user"})

    primary = (
        state.rewritten_queries[0] if state.rewritten_queries else state.user_input or ""
    ).strip()
    if not primary:
        return {"rewritten_queries": list(state.rewritten_queries)}

    prompt = render("multi_query", rewritten_query=primary)
    try:
        # LIGHT 模型 (mimo-v2.5) 是 reasoning model：早期 max_tokens=400 在复杂查询上
        # 被 reasoning 吃满（实测 reasoning=399, content=''）→ 返回空 sub_queries，
        # complex 链路退化为单 query 检索，最该多角度展开的场景反而展不开。
        # 2048 给 reasoning ~1000 + JSON 数组 ~300 + 余量，远超观测峰值（reasoning ~400）。
        resp = await deps.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=deps.settings.LLM_LIGHT_MODEL,
            temperature=0.2,
            max_tokens=2048,
        )
    except LLMError as exc:
        log.warning("multi_query_node llm failed: %s", exc)
        return {"rewritten_queries": list(state.rewritten_queries) or [primary]}

    sub_queries = _parse_sub_queries(resp)
    merged: list[str] = [primary]
    seen = {primary.lower()}
    for q in sub_queries:
        qn = q.strip()
        if not qn:
            continue
        key = qn.lower()
        if key in seen:
            continue
        merged.append(qn)
        seen.add(key)
        if len(merged) >= 1 + _MAX_SUB_QUERIES:
            break
    return {"rewritten_queries": merged}


_JSON_ARRAY_RE = re.compile(r"\[\s*(?:\".*?\"\s*,?\s*)+\]", re.S)


def _parse_sub_queries(resp: dict[str, Any]) -> list[str]:
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return []
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return []
    text = content.strip()
    if not text:
        return []
    # 优先：整段就是 JSON array
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, (str, int, float))]
    except json.JSONDecodeError:
        pass
    # 兜底：从文本里抠 JSON array
    m = _JSON_ARRAY_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [str(x) for x in data if isinstance(x, (str, int, float))]
        except json.JSONDecodeError:
            pass
    # 再兜底：按行拆，每行去掉引号 / bullet
    lines = []
    for ln in text.splitlines():
        s = ln.strip().lstrip("-*0123456789.) ").strip().strip('"').strip()
        if s:
            lines.append(s)
    return lines
