"""单测：会话首轮自动标题 `generate_session_title`。"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.session_title import MAX_TITLE_CHARS, generate_session_title


class _FakeClient:
    def __init__(self, content: Any) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"messages": messages, **kwargs})
        return {"choices": [{"message": {"content": self._content}}]}


class _BoomClient:
    async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("llm down")


async def test_generates_clean_title() -> None:
    cli = _FakeClient("AMF 注册流程")
    title = await generate_session_title(
        question="5G 中 AMF 的注册流程是怎样的？", chat_client=cli, model="light"
    )
    assert title == "AMF 注册流程"
    # 用 LIGHT model + thinking=disabled 调用；thinking 关掉后 reasoning_tokens=0
    # 且 temperature=0 真生效，max_tokens 回归小值即够。M5 反复修不生效的真因是
    # 思考模式下 reasoning 吃光 max_tokens + temp=0 被强制 1.0；本断言锁住两条。
    assert cli.calls[0]["model"] == "light"
    assert cli.calls[0]["max_tokens"] >= 512
    assert cli.calls[0]["thinking"] == {"type": "disabled"}


async def test_strips_quotes_and_extra_lines() -> None:
    cli = _FakeClient('"BWP 切换"\n（其它解释）')
    title = await generate_session_title(question="什么是 BWP 切换", chat_client=cli, model="m")
    assert title == "BWP 切换"


async def test_truncates_to_max_chars() -> None:
    cli = _FakeClient("x" * 200)
    title = await generate_session_title(question="q", chat_client=cli, model="m")
    assert title is not None
    assert len(title) == MAX_TITLE_CHARS


@pytest.mark.parametrize("question", ["", "   "])
async def test_empty_question_returns_none(question: str) -> None:
    cli = _FakeClient("anything")
    title = await generate_session_title(question=question, chat_client=cli, model="m")
    assert title is None
    # 空问题不应触发 LLM 调用
    assert cli.calls == []


async def test_llm_error_returns_none() -> None:
    title = await generate_session_title(question="q", chat_client=_BoomClient(), model="m")
    assert title is None


async def test_blank_llm_output_returns_none() -> None:
    cli = _FakeClient("   ")
    title = await generate_session_title(question="q", chat_client=cli, model="m")
    assert title is None
