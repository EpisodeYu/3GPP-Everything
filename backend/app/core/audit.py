"""审计日志写入辅助。

文档锚点：`docs/03-development/04-backend-api.md §5 / §7.2`。

约束（Q5 决策）：
- **chat 消息正文 / SSE token 流不写 audit_logs**；chat 路径调用方需自我克制
  (本模块不主动过滤，因为粒度难判定，靠调用方在 §M4.6 触发点只写鉴权 / admin /
  会话删除 / 反馈这些动作)。
- secret 原文（token / 密码）禁止入 metadata；调用方传 hash 前缀或 jti 即可。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


async def write_audit(
    db: AsyncSession,
    *,
    actor_user_id: uuid.UUID | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AuditLog:
    """写一行 audit_logs。

    不在这里 commit —— 由调用方所在请求统一 commit（避免与业务事务半提交分裂）。
    """
    row = AuditLog(
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        ip=ip,
        user_agent=user_agent,
        extra=dict(extra or {}),
    )
    db.add(row)
    await db.flush()
    return row
