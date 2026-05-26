"""会话首轮自动标题（M5.x）。

会话以空 `title` 创建（前端 `createBlank`）。首轮成功回答后，用 LIGHT 模型把用户
的首个问题压成一句短标题，回写 `session.title`，并由 chat SSE `title` 事件通知前端
sidebar 即时更新（见 `app/api/v1/chat.py` + `docs/03-development/03-agent.md §7`）。

设计取舍：
- 只用「用户首个问题」生成标题，不喂答案：问题是会话主题的最强信号，prompt 更短更快；
  也避免把整段答案塞进 LIGHT 模型。
- 任何失败（LLM 报错 / 空输出）都返回 None，由 caller 跳过回写，标题保持空 →
  前端 fallback 显示「新会话」，下一轮还会再尝试，自愈。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

log = logging.getLogger(__name__)

# 标题落库上限远小于 DB 的 255，控制 sidebar 展示长度。
MAX_TITLE_CHARS = 60

_TITLE_PROMPT = """\
You generate a very short title summarizing the TOPIC of a 3GPP-domain user
question. Reply in the SAME language as the question.

Rules:
- Output ONLY the title — no quotes, no surrounding punctuation, no commentary.
- A concise noun phrase, not a full sentence.
- <= 12 words, or <= 20 Chinese characters.
- Keep proper nouns (AMF, SMF, 5G-AKA, gNB, NG-RAN, RRC, etc.).

User question:
{question}
"""


class _ChatClient(Protocol):
    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = ...,
        temperature: float | None = ...,
        max_tokens: int | None = ...,
    ) -> dict[str, Any]: ...


def _clean(raw: str) -> str:
    """取首行、去引号、截断到上限。"""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    line = lines[0] if lines else ""
    line = line.strip().strip('"').strip("'").strip()
    if len(line) > MAX_TITLE_CHARS:
        line = line[:MAX_TITLE_CHARS].rstrip()
    return line


async def generate_session_title(
    *, question: str, chat_client: _ChatClient, model: str
) -> str | None:
    """从用户首个问题生成短标题；任何失败返回 None（caller 跳过回写）。"""
    q = (question or "").strip()
    if not q:
        return None
    try:
        resp = await chat_client.chat(
            messages=[{"role": "user", "content": _TITLE_PROMPT.format(question=q[:2000])}],
            model=model,
            temperature=0.0,
            max_tokens=40,
        )
        content = resp["choices"][0]["message"]["content"]
    except Exception as exc:  # LLM / 解析失败都不应影响主流程
        log.debug("generate_session_title failed: %s", exc)
        return None
    if not isinstance(content, str):
        return None
    return _clean(content) or None
