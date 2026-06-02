"""Admin schemas（M4.10 `/api/v1/admin/*` 路由）。

文档锚点：`docs/03-development/04-backend-api.md §2 Admin / §9.1`。

口径：
- `IndexRebuildBody.spec_id=None` 视作"全量重建"（payload 透传给 ingestion CLI）
- `TaskOut` 字段口径与 `db.models.Task` 一一对齐
- `StatsOut.tasks` 按 `status` 分桶；`api_usage_7d` 取最近 7 天聚合
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.messages import MessageOut

TaskKind = Literal["crawl", "index_rebuild"]
TaskStatus = Literal["queued", "running", "done", "failed"]


class IndexRebuildBody(BaseModel):
    """Trigger admin/index/rebuild。

    - `spec_id=None` → 全量重建（透传到 ingestion CLI 的全量命令）
    - `force=True` → 在已有索引上强制重跑（CLI 端按 purge_first 处理）
    """

    spec_id: str | None = Field(default=None, max_length=32)
    force: bool = False


class TaskOut(BaseModel):
    id: uuid.UUID
    kind: TaskKind
    payload: dict[str, Any]
    status: TaskStatus
    progress: int
    log_tail: str
    started_at: datetime | None
    finished_at: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskOut]
    total: int


class ApiUsage7dOut(BaseModel):
    llm_input_tokens: int
    llm_output_tokens: int
    embedding_tokens: int
    rerank_calls: int
    web_search_calls: int
    total_cost_usd: float


class FeedbackStatsOut(BaseModel):
    """`/admin/feedback` 的全量计数（不受 thumb filter / 分页影响）。"""

    up: int
    down: int
    total: int


class AdminFeedbackItem(BaseModel):
    """单条反馈 + 关联消息预览 / 反馈者 / 会话定位。"""

    id: uuid.UUID
    message_id: uuid.UUID
    session_id: uuid.UUID | None
    thumb: int
    reason: str | None
    username: str | None
    message_preview: str | None
    created_at: datetime


class AdminFeedbackListResponse(BaseModel):
    stats: FeedbackStatsOut
    items: list[AdminFeedbackItem]
    total: int


class AdminSessionDetailOut(BaseModel):
    """admin 查看任意用户会话的完整消息（含引用）— 用于反馈溯源/针对性优化。"""

    id: uuid.UUID
    title: str
    username: str | None  # 会话归属者
    created_at: datetime
    messages: list[MessageOut]


class StatsOut(BaseModel):
    """`/admin/stats` 返回口径。

    - `documents` 来自 `documents` 表（M4.9 reader 起 ingestion 写入；M4 阶段可能为 0）
    - `chunks` 来自 `chunks_meta`（ingestion 唯一写者，M6 全量后 ~395k）
    - `tasks` 按 status 分桶
    - `api_usage_7d` 取最近 7 天 `api_usage` 表的聚合
    """

    documents: int
    chunks: int
    users: int
    sessions: int
    messages: int
    tasks: dict[str, int]
    api_usage_7d: ApiUsage7dOut
