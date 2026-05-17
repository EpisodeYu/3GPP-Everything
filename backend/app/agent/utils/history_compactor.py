"""会话历史压缩器（M4.7 Q10）。

口径 `docs/04-handoff/2026-05-17-m4.6-m4.9-decisions.md §一 Q10`：

- 会话 `message_count <= 8`：不压缩，直接全量取近 N=6 user/assistant
- 会话 `message_count > 8`：
  - 取最近 N=6 条原文
  - 更早消息按时间顺序拼成单段，交 `mimo-v2.5`（非 pro，省成本）做单段 summary
  - summary 注入到 LangGraph state 的 `system` 前缀（一段 SystemMessage）
- summary 缓存：Redis key `tgpp:cache:history_summary:{session_id}:{last_message_id}`，
  TTL 24h；相同 (session_id, last_message_id) 第二次直接复用

外部依赖：
- `LiteLLMClient.chat`（任何 OpenAI 兼容 client，单元测可注入 stub）
- `redis.asyncio.Redis`（接口仅用 `get` / `setex`；conftest FakeRedis 已覆盖）

调用方：M4.7 chat 路由入口在构造 AgentState 时调 `compact_history()`，把返回的
`messages` 列表喂进 state.messages（含 SystemMessage summary + 最近 N 条）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

log = logging.getLogger(__name__)

# 自主决策（CLAUDE.md §4.3）
RECENT_N: int = 6
COMPACT_THRESHOLD: int = 8
SUMMARY_TTL_S: int = 24 * 3600
SUMMARY_MODEL: str = "mimo-v2.5"
KEY_PREFIX: str = "tgpp:cache:history_summary"


@dataclass(frozen=True)
class HistoryMessage:
    """精简的历史消息表示，避免直接耦合 SQLAlchemy Message ORM。"""

    id: uuid.UUID
    role: str  # "user" / "assistant" / "system"
    content: str


class ChatClient(Protocol):
    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class RedisLike(Protocol):
    async def get(self, key: str) -> Any: ...
    async def setex(self, key: str, ttl: int, value: str) -> Any: ...


def _to_lc_message(m: HistoryMessage) -> BaseMessage:
    if m.role == "user":
        return HumanMessage(content=m.content)
    if m.role == "assistant":
        return AIMessage(content=m.content)
    return SystemMessage(content=m.content)


def _summary_key(session_id: uuid.UUID | str, last_message_id: uuid.UUID | str) -> str:
    return f"{KEY_PREFIX}:{session_id}:{last_message_id}"


def _extract_content(resp: dict[str, Any]) -> str:
    choices = resp.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "").strip()


async def compact_history(
    history: Sequence[HistoryMessage],
    *,
    session_id: uuid.UUID | str,
    chat_client: ChatClient,
    redis: RedisLike | None = None,
    recent_n: int = RECENT_N,
    threshold: int = COMPACT_THRESHOLD,
) -> list[BaseMessage]:
    """压缩历史并返回喂进 LangGraph 的 messages 列表。

    返回顺序：[SystemMessage(summary)?, HumanMessage / AIMessage * recent_n]。

    `history` 应按时间升序传入；caller 拿到 DB 行后排好序再传。
    """
    if not history:
        return []

    if len(history) <= threshold:
        return [_to_lc_message(m) for m in history[-recent_n:]]

    older = list(history[:-recent_n])
    recent = list(history[-recent_n:])

    summary_text: str | None = None
    last_id = older[-1].id

    if redis is not None:
        cached = await redis.get(_summary_key(session_id, last_id))
        if cached:
            summary_text = cached if isinstance(cached, str) else cached.decode("utf-8")

    if summary_text is None:
        prompt = _build_summary_prompt(older)
        try:
            resp = await chat_client.chat(
                messages=prompt,
                model=SUMMARY_MODEL,
                temperature=0.0,
                max_tokens=400,
            )
            summary_text = _extract_content(resp)
        except Exception as exc:
            # summary 失败不应阻塞主路径；降级为不压缩，只丢老消息（保留 recent）
            log.warning("history_compactor summary failed, drop older: %s", exc)
            return [_to_lc_message(m) for m in recent]

        if redis is not None and summary_text:
            try:
                await redis.setex(_summary_key(session_id, last_id), SUMMARY_TTL_S, summary_text)
            except Exception as exc:
                log.warning("history_compactor cache.setex failed: %s", exc)

    messages: list[BaseMessage] = []
    if summary_text:
        messages.append(SystemMessage(content=f"[Conversation summary]\n{summary_text}"))
    messages.extend(_to_lc_message(m) for m in recent)
    return messages


def _build_summary_prompt(older: Sequence[HistoryMessage]) -> list[dict[str, str]]:
    transcript_lines: list[str] = []
    for m in older:
        prefix = m.role.upper()
        transcript_lines.append(f"{prefix}: {m.content}")
    transcript = "\n".join(transcript_lines)
    return [
        {
            "role": "system",
            "content": (
                "You compress a 3GPP/telecom Q&A conversation into a concise factual "
                "summary in <= 200 English words. Preserve spec ids (e.g. 23.501), "
                "section paths, and concrete decisions. Drop pleasantries."
            ),
        },
        {"role": "user", "content": transcript},
    ]
