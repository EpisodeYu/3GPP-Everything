"""Pydantic v2 schemas for /chat 路由（M4.7）。

仅 request body — SSE 响应是流式 event:/data: 文本，不走 Pydantic 序列化。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# raw_lookup 模式已下线，仅保留 qa。字段保留以兼容历史数据/接口形状。
Mode = Literal["qa"]


class SendMessageBody(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)
    mode: Mode | None = None
    explicit_tools: list[str] = Field(default_factory=list)
