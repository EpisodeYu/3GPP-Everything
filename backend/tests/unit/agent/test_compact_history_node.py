"""compact_history_node：图内会话历史压缩（2026-06-02 对齐 §6.1「由 deps 注入」）。

口径见 docs/03-development/03-agent.md §6.1。验证：
- 无 raw_history → history=[]，不调 LLM
- 短历史（<= 阈值）→ 不调 summary LLM，直接取最近 N 条原文（role/content）
- 长历史（> 阈值）→ 调 deps.llm 出 summary，结果含 system + 最近 N 条
- summary 失败 → 降级保留最近 N 条（compact_history 内部兜底）
"""

from __future__ import annotations

import uuid

from app.agent.nodes import compact_history_node
from app.agent.state import AgentState
from app.agent.utils.history_compactor import COMPACT_THRESHOLD, RECENT_N

from .conftest import StubLLM, make_deps


def _raw(n: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"id": str(uuid.uuid4()), "role": role, "content": f"msg {i}"})
    return out


async def test_no_raw_history_returns_empty_and_no_llm() -> None:
    llm = StubLLM(responses=["should NOT be called"])
    deps = make_deps(llm=llm)
    out = await compact_history_node(AgentState(user_input="q"), deps=deps)
    assert out["history"] == []
    assert not [c for c in llm.calls if c["kind"] in ("chat", "chat_stream")]


async def test_short_history_keeps_recent_without_summary_llm() -> None:
    llm = StubLLM(responses=["should NOT be called"])
    deps = make_deps(llm=llm)
    raw = _raw(COMPACT_THRESHOLD)  # <= 阈值 → 不 summary
    state = AgentState(user_input="follow up", raw_history=raw, session_id="s1")

    out = await compact_history_node(state, deps=deps)
    hist = out["history"]
    # 取最近 N 条（RECENT_N），形态只剩 role/content（id 被剥）
    assert len(hist) == min(RECENT_N, COMPACT_THRESHOLD)
    assert all(set(h.keys()) == {"role", "content"} for h in hist)
    assert not [c for c in llm.calls if c["kind"] == "chat"]


async def test_long_history_calls_summary_llm_and_prepends_system() -> None:
    llm = StubLLM(responses=["SUMMARY: earlier turns about PUCCH-Config."])
    deps = make_deps(llm=llm)
    raw = _raw(COMPACT_THRESHOLD + 4)  # > 阈值 → 触发 summary
    state = AgentState(user_input="follow up", raw_history=raw, session_id="s1")

    out = await compact_history_node(state, deps=deps)
    hist = out["history"]
    # summary（system）+ 最近 N 条
    assert hist[0]["role"] == "system"
    assert "SUMMARY" in hist[0]["content"]
    assert len(hist) == 1 + RECENT_N
    # 调了一次 summary chat（thinking=disabled）
    summary_calls = [c for c in llm.calls if c["kind"] == "chat"]
    assert len(summary_calls) == 1
    assert summary_calls[0]["thinking"] == {"type": "disabled"}
