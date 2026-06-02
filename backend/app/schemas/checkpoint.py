"""Pydantic v2 schemas for /sessions/{sid}/checkpoints + pause/resume/fork/rollback（M4.8）。"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.sessions import SessionOut


class CheckpointOut(BaseModel):
    checkpoint_id: str
    parent_checkpoint_id: str | None
    created_at: str
    next_nodes: list[str]
    last_node: str | None


class CheckpointListResponse(BaseModel):
    items: list[CheckpointOut]


class ForkBody(BaseModel):
    checkpoint_id: str = Field(min_length=1)
    new_user_message: str | None = Field(default=None, max_length=4000)
    title: str | None = Field(default=None, max_length=255)
    # 精准分叉（2026-06-02）：被点 user 消息 id。复制历史只截到该消息所在回合末尾
    # （含其答案）。None → 复制全部历史（向后兼容 / 等价从最后一轮分叉）。
    up_to_message_id: uuid.UUID | None = Field(default=None)


class ForkResponse(BaseModel):
    new_session: SessionOut


class RollbackBody(BaseModel):
    """`POST /sessions/{sid}/rollback` body。

    `last_n` = **轮数**（"一轮" = 一个 user message + 它之后该会话的所有
    message）。语义在 2026-06-01 与 UI 文案对齐；旧实现按 "条数" 删，且 PG
    同事务下 user/assistant 同 created_at 时排序不稳定，导致只删 user 留 assistant。
    详见 `app.api.v1.checkpoint.rollback_session` docstring。
    """

    last_n: int = Field(ge=1, le=500)


class RollbackResponse(BaseModel):
    deleted_messages: int
    head_checkpoint_id: str | None


class PauseResponse(BaseModel):
    run_id: str
    session_id: uuid.UUID
    status: str = "paused"
