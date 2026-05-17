"""限流：Redis 令牌桶（按用户 + bucket）。

文档锚点：`docs/03-development/04-backend-api.md §6`。

bucket 与默认阈值（单位都是"在 `period_s` 秒内最多允许的请求数"）：
- `chat`：60 req / 1h
- `tools_websearch`：20 calls / 1d（Tavily 成本控制）
- `admin_crawl`：5 calls / 1d

实现：定窗 INCR + EXPIRE，简单可靠；不上漏桶（多用户场景 60/h 粒度的定窗误差可忽略）。

Key 形如 `tgpp:rl:{user_id}:{bucket}:{window_start}`，window_start 用 `now // period`
做整数桶；EXPIRE 由首次 INCR 时设置（NX EX）。

依赖注入：`get_redis()` 从 settings 读 `REDIS_URL`，单例。测试可 monkey-patch 或
直接传 `client=` 走 in-memory stub。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, Request

from app.core.config import get_settings
from app.core.errors import RateLimitedError
from app.db.models import User

# Redis namespace 前缀
_KEY_PREFIX = "tgpp:rl"


@dataclass(frozen=True)
class BucketSpec:
    name: str
    limit: int
    period_s: int


# bucket 定义。改阈值同步 04-backend-api.md §6。
BUCKETS: dict[str, BucketSpec] = {
    "chat": BucketSpec("chat", 60, 3600),
    "tools_websearch": BucketSpec("tools_websearch", 20, 86400),
    "admin_crawl": BucketSpec("admin_crawl", 5, 86400),
}


# === Redis 客户端单例 ===


@lru_cache(maxsize=1)
def _default_client() -> aioredis.Redis:
    s = get_settings()
    return aioredis.from_url(s.REDIS_URL, decode_responses=True)


def get_redis() -> aioredis.Redis:
    return _default_client()


def reset_redis_singleton() -> None:
    """测试用：清掉 lru_cache，让下次 get_redis 重新构造。"""
    _default_client.cache_clear()


# === 核心算法 ===


def _key(user_id: uuid.UUID | str, bucket: str, *, now: float | None = None) -> str:
    spec = BUCKETS[bucket]
    t = int(now if now is not None else time.time())
    window = t // spec.period_s
    return f"{_KEY_PREFIX}:{user_id}:{bucket}:{window}"


async def consume(
    client: Any,
    *,
    user_id: uuid.UUID | str,
    bucket: str,
    now: float | None = None,
) -> int:
    """+1 当前窗口计数；超过 limit 抛 RateLimitedError；返回当前计数。

    client 接受任何带 `incr` / `expire` async 方法的对象（生产是 aioredis.Redis，
    测试可注入 stub）。
    """
    if bucket not in BUCKETS:
        raise KeyError(f"unknown ratelimit bucket: {bucket}")
    spec = BUCKETS[bucket]
    key = _key(user_id, bucket, now=now)
    count = int(await client.incr(key))
    if count == 1:
        # 首次落到这个窗口，挂 TTL；TTL 超时后该 key 自动消失
        await client.expire(key, spec.period_s)
    if count > spec.limit:
        raise RateLimitedError(
            f"rate_limited:{bucket}",
            code="rate_limited",
            details={"bucket": bucket, "limit": spec.limit, "period_s": spec.period_s},
        )
    return count


# === FastAPI 依赖工厂 ===


def rate_limit(bucket: str):
    """生成 Depends：对当前 user + bucket 做 consume。

    路由侧：
        @router.post("/chat", dependencies=[Depends(rate_limit("chat"))])
    或者要拿计数：
        count = await rate_limit_check(user, "chat")
    """
    from app.core.auth import get_current_user  # 延迟 import 避免循环

    async def _dep(
        request: Request,
        user: User = Depends(get_current_user),
    ) -> None:
        client = getattr(request.app.state, "redis", None) or get_redis()
        await consume(client, user_id=user.id, bucket=bucket)

    return _dep
