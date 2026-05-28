"""单元测：history_compactor（M4.7 Q10）。

覆盖 4 条路径：
- 空 history → []
- N <= threshold → 只取最近 N 条原文
- N > threshold + Redis cache miss → 调 LLM，写缓存，返回 [Summary, *recent]
- N > threshold + Redis cache hit → 跳过 LLM，直接返回 [Summary(cached), *recent]
- LLM 失败 → 降级到 recent only
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.utils.history_compactor import (
    COMPACT_THRESHOLD,
    KEY_PREFIX,
    RECENT_N,
    SUMMARY_TTL_S,
    HistoryMessage,
    compact_history,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.get_calls: list[str] = []
        self.setex_calls: list[tuple[str, int, str]] = []

    async def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self.kv.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self.kv[key] = value
        self.ttls[key] = ttl
        self.setex_calls.append((key, ttl, value))
        return True


class _StubChat:
    """返回固定 summary 文本的 chat client；可强制 raise 模拟 LLM 失败。"""

    def __init__(self, *, content: str = "SUMMARY-OK", raise_exc: bool = False) -> None:
        self.content = content
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"messages": messages, **kwargs})
        if self.raise_exc:
            raise RuntimeError("llm_boom")
        return {"choices": [{"message": {"content": self.content}}]}


def _mk_history(n: int) -> list[HistoryMessage]:
    out: list[HistoryMessage] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(HistoryMessage(id=uuid.uuid4(), role=role, content=f"msg-{i}"))
    return out


@pytest.mark.unit
async def test_empty_history_returns_empty() -> None:
    out = await compact_history([], session_id=uuid.uuid4(), chat_client=_StubChat())
    assert out == []


@pytest.mark.unit
async def test_below_threshold_returns_recent_only_no_summary() -> None:
    redis = _FakeRedis()
    chat = _StubChat()
    history = _mk_history(COMPACT_THRESHOLD)  # 恰好 == threshold，不压缩
    out = await compact_history(history, session_id=uuid.uuid4(), chat_client=chat, redis=redis)
    assert chat.calls == []  # 未触发 LLM
    assert redis.get_calls == []  # 未查缓存
    assert len(out) == RECENT_N
    assert all(not isinstance(m, SystemMessage) for m in out)
    # 应取最后 N 条
    assert out[-1].content == history[-1].content


@pytest.mark.unit
async def test_above_threshold_cache_miss_calls_llm_and_writes_cache() -> None:
    redis = _FakeRedis()
    chat = _StubChat(content="SUMMARY-FRESH")
    history = _mk_history(COMPACT_THRESHOLD + 4)
    sid = uuid.uuid4()

    out = await compact_history(history, session_id=sid, chat_client=chat, redis=redis)

    assert len(chat.calls) == 1
    # summary 是事实压缩任务，mimo 思考模式下 temp=0 会被强制 1.0 → 同 older 不同
    # summary，cache 钉死后内涵不可复现。锁住 thinking=disabled 让 temp=0 真生效。
    assert chat.calls[0]["thinking"] == {"type": "disabled"}
    older = history[:-RECENT_N]
    last_id = older[-1].id
    expected_key = f"{KEY_PREFIX}:{sid}:{last_id}"
    assert redis.setex_calls == [(expected_key, SUMMARY_TTL_S, "SUMMARY-FRESH")]

    assert isinstance(out[0], SystemMessage)
    assert "SUMMARY-FRESH" in out[0].content
    assert len(out) == 1 + RECENT_N
    assert isinstance(out[1], HumanMessage | AIMessage)


@pytest.mark.unit
async def test_above_threshold_cache_hit_skips_llm() -> None:
    redis = _FakeRedis()
    history = _mk_history(COMPACT_THRESHOLD + 5)
    sid = uuid.uuid4()
    older = history[:-RECENT_N]
    cache_key = f"{KEY_PREFIX}:{sid}:{older[-1].id}"
    redis.kv[cache_key] = "CACHED-SUMMARY"

    chat = _StubChat(content="SHOULD-NOT-BE-CALLED")
    out = await compact_history(history, session_id=sid, chat_client=chat, redis=redis)

    assert chat.calls == []
    assert isinstance(out[0], SystemMessage)
    assert "CACHED-SUMMARY" in out[0].content
    assert redis.setex_calls == []  # cache hit 不应再写


@pytest.mark.unit
async def test_llm_failure_falls_back_to_recent_only() -> None:
    redis = _FakeRedis()
    chat = _StubChat(raise_exc=True)
    history = _mk_history(COMPACT_THRESHOLD + 3)

    out = await compact_history(history, session_id=uuid.uuid4(), chat_client=chat, redis=redis)
    assert all(not isinstance(m, SystemMessage) for m in out)
    assert len(out) == RECENT_N
    assert redis.setex_calls == []
