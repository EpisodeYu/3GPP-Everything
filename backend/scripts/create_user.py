#!/usr/bin/env python
"""创建 / 确保一个用户存在（幂等）—— 运维 / 部署用。

用法（容器内，WORKDIR=/app）：
    PYTHONPATH=/app python scripts/create_user.py <username> <password> [role]

- role 缺省 `user`，可选 `user` | `admin`。
- 已存在同名用户 → 跳过（**不**改密码 / 角色），exit 0。
- 走 app 自己的 `DATABASE_URL` + bcrypt `hash_password`，与运行时一致。
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.auth import hash_password
from app.db.base import get_sessionmaker
from app.db.models import User

_VALID_ROLES = ("user", "admin")


async def _create_user(username: str, password: str, role: str) -> int:
    sm = get_sessionmaker()
    async with sm() as db:
        existing = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"[create_user] 用户已存在，跳过：{username}（role={existing.role}）")
            return 0
        db.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
            )
        )
        await db.commit()
    print(f"[create_user] 已创建用户：{username}（role={role}）")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "用法: python scripts/create_user.py <username> <password> [role]",
            file=sys.stderr,
        )
        return 2
    username, password = sys.argv[1], sys.argv[2]
    role = sys.argv[3] if len(sys.argv) > 3 else "user"
    if role not in _VALID_ROLES:
        print(f"[create_user] role 必须是 {_VALID_ROLES}，收到：{role}", file=sys.stderr)
        return 2
    return asyncio.run(_create_user(username, password, role))


if __name__ == "__main__":
    sys.exit(main())
