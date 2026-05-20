"""SSE 帧 / 事件解析，用于 eval runner 解析 `/api/v1/sessions/{sid}/messages` 的流。

设计：抽出 `backend/tests/integration/api/test_chat.py::_parse_sse` 同样逻辑，独立
放进 eval 避免循环依赖（eval 不依赖 backend）。

支持两种形态：

1. 一次性整段解析（`parse_sse_text(text) -> list[SSEEvent]`）
   适合：buffer 完整响应后离线解析（test_chat 同款）

2. 流式行解析（`SSEStreamParser`）
   适合：httpx 的 `aiter_lines()` / `aiter_text()` 真实流式场景；逐行 feed，回吐
   完整事件迭代器；让 runner 不必等响应结束

SSE 帧约定（HTML5 SSE / sse-starlette）：
- 行以 `event:` 或 `data:` 开头；多 `data:` 同帧用换行拼接
- 帧之间空行分隔；以 `:` 开头是注释（sse-starlette ping 用），忽略
- 不解析 `id:` / `retry:`（项目未用）
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SSEEvent:
    """单条 SSE 事件。data 是原始字符串（JSON 时由调用方 .parse）。"""

    event: str
    data: str

    def parse_json(self) -> dict:
        """data 解析为 JSON dict；失败 raise ValueError（不静默吞）。"""
        try:
            obj = json.loads(self.data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"sse event {self.event!r} data not json: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"sse event {self.event!r} data not json dict: {type(obj)}")
        return obj


def parse_sse_text(text: str) -> list[SSEEvent]:
    """切完整 SSE payload → 事件列表；忽略 ping 注释行。

    与 `backend/tests/integration/api/test_chat.py::_parse_sse` 行为完全一致。
    """
    out: list[SSEEvent] = []
    event: str | None = None
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if event is not None:
                out.append(SSEEvent(event=event, data="\n".join(data_lines)))
            event = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue  # ping 注释
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if event is not None:
        out.append(SSEEvent(event=event, data="\n".join(data_lines)))
    return out


class SSEStreamParser:
    """逐行 feed SSE 流；调 `events()` 拿到当前 buffer 里完整的事件并清空。

    用法（与 httpx.AsyncClient.stream 配合）::

        parser = SSEStreamParser()
        async with client.stream("POST", url, json=body) as resp:
            async for line in resp.aiter_lines():
                parser.feed(line)
                for ev in parser.drain():
                    handle(ev)
        for ev in parser.drain():        # buffer 残留
            handle(ev)
    """

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []
        self._ready: list[SSEEvent] = []

    def feed(self, line: str) -> None:
        """处理一行；可能完成一帧并存进 _ready。"""
        # httpx aiter_lines 返回的行不含末尾 \n
        if not line.strip():
            if self._event is not None:
                self._ready.append(SSEEvent(event=self._event, data="\n".join(self._data)))
            self._event = None
            self._data = []
            return
        if line.startswith(":"):
            return
        if line.startswith("event:"):
            self._event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            self._data.append(line[len("data:"):].lstrip())

    def drain(self) -> Iterator[SSEEvent]:
        """yield 当前 buffer 中已完成的事件，并清空。"""
        out, self._ready = self._ready, []
        yield from out

    def close(self) -> Iterable[SSEEvent]:
        """流结束：把残留未刷出的最后一帧（若末尾没空行）补刷。"""
        if self._event is not None:
            self._ready.append(SSEEvent(event=self._event, data="\n".join(self._data)))
            self._event = None
            self._data = []
        return list(self.drain())


__all__ = [
    "SSEEvent",
    "SSEStreamParser",
    "parse_sse_text",
]
