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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from fastapi import Depends, Request

from app.core.config import Settings, get_settings
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
    # 登录 / 凭据端点暴力破解防护：按客户端 IP（非 user）限，预鉴权。10 次 / 5min。
    "login": BucketSpec("login", 10, 300),
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


def _client_ip(request: Request) -> str:
    """限流用的真实客户端 IP。

    生产在 ingress(nginx) 之后：ingress 用 `proxy_set_header X-Real-IP $remote_addr`
    把连接对端 IP 强制写入（客户端伪造的同名 header 被覆盖），可安全作为限流 key。
    **不**取 `X-Forwarded-For` leftmost：ingress 用 `$proxy_add_x_forwarded_for` 会把
    客户端自带的 XFF 前置，leftmost 可被伪造，不能用于安全限流。dev 直连无 X-Real-IP
    → 退回 ASGI peer host。
    """
    xri = request.headers.get("x-real-ip")
    if xri and xri.strip():
        return xri.strip()
    return request.client.host if request.client else "unknown"


async def login_rate_limit(request: Request) -> None:
    """预鉴权登录限流：按客户端 IP 限 `login` bucket，挡暴力破解 / 撞库。

    与 `rate_limit()` 的区别：**不**依赖 `get_current_user`（登录前还没有 user），
    按 IP 计数。挂在 `/auth/login` 与 `/auth/bootstrap-admin` 这两个 pre-auth
    凭据端点；成功 / 失败都计数（计的是"尝试次数"）。
    """
    client = getattr(request.app.state, "redis", None) or get_redis()
    await consume(client, user_id=_client_ip(request), bucket="login")


# === 普通用户每日对话配额（role=="user"；admin 豁免）===
#
# 与上面的定窗令牌桶同一 Redis namespace，但**按 APP_TIMEZONE 本地日期**分桶
# （key 里直接带 YYYYMMDD），保证「每天 0 点」按本地时区切换、多 worker 一致。
# 计数即在 chat 路由依赖里发生（与 chat bucket 同口径：每次 POST 计 1 次，请求
# 即便后续 404 / agent 失败也计数）。边界事件交给注入的 `on_event` 回调（生产侧
# 即 Server酱推送）：
#   - 当日首次（count==1）         → on_event("first")
#   - 首次越界（count==limit+1）   → on_event("over") + 抛 RateLimitedError
# 之后的越界请求只抛错、不再重复通知（每天每用户最多各推一次 first / over）。

DAILY_CHAT_BUCKET = "chat_daily"

# on_event 回调签名：(kind, user, count, limit) -> awaitable
QuotaEvent = Callable[[str, User, int, int], Awaitable[None]]


def _safe_zoneinfo(tz: str) -> ZoneInfo:
    """非法时区名兜底到 UTC（与 alerts scheduler 容错口径一致）。"""
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo("UTC")


def _daily_key(
    user_id: uuid.UUID | str, *, tz: str, now: datetime | None = None
) -> tuple[str, int]:
    """返回 (redis_key, ttl_s)：按本地日期分桶；ttl = 到本地次日 0 点的秒数 + 1h buffer。

    日期已写进 key，跨日自然换桶，ttl 仅用于回收旧 key，不影响正确性。
    """
    z = _safe_zoneinfo(tz)
    local_now = now.astimezone(z) if now is not None else datetime.now(z)
    key = f"{_KEY_PREFIX}:{user_id}:{DAILY_CHAT_BUCKET}:{local_now.strftime('%Y%m%d')}"
    tomorrow = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ttl = int((tomorrow - local_now).total_seconds()) + 3600
    return key, ttl


async def enforce_daily_chat_quota(
    client: Any,
    *,
    user: User,
    settings: Settings | None = None,
    now: datetime | None = None,
    on_event: QuotaEvent | None = None,
) -> int | None:
    """普通用户每日对话配额检查 + 计数。

    - `user.role != "user"`（admin 等）→ 豁免：不计数、不通知、返回 None。
    - `DAILY_CHAT_LIMIT <= 0` → 关闭：返回 None。
    - 否则 +1 当日计数；命中 count==1 / count==limit+1 触发 `on_event`；
      count>limit 抛 `RateLimitedError`（status 429）。返回当前计数。
    """
    s = settings or get_settings()
    limit = s.DAILY_CHAT_LIMIT
    if limit <= 0 or user.role != "user":
        return None

    key, ttl = _daily_key(user.id, tz=s.APP_TIMEZONE, now=now)
    count = int(await client.incr(key))
    if count == 1:
        await client.expire(key, ttl)

    if on_event is not None:
        if count == 1:
            await on_event("first", user, count, limit)
        elif count == limit + 1:
            await on_event("over", user, count, limit)

    if count > limit:
        raise RateLimitedError(
            "daily_chat_quota_exceeded",
            code="rate_limited",
            details={
                "bucket": DAILY_CHAT_BUCKET,
                "limit": limit,
                "period": "day",
                "count": count,
            },
        )
    return count


async def _serverchan_quota_event(
    kind: str, user: User, count: int, limit: int, *, settings: Settings
) -> None:
    """生产 on_event：把边界事件 fire-and-forget 推到 Server酱（不阻塞请求）。

    用请求级注入的 `settings.SERVERCHAN_URL`（而非全局 get_settings），便于 dependency
    override / 测试隔离 —— 集成测的空 URL 即静音，不会误发真实推送。
    """
    from app.services.notify import schedule_serverchan

    if kind == "first":
        title = f"[3GPP] {user.username} 今日首次对话"
        desp = (
            f"用户 **{user.username}**（id `{user.id}`）今天第 1 次对话。\n\n"
            f"每日对话上限：**{limit}** 次。"
        )
    else:  # "over"
        title = f"[3GPP] {user.username} 超出每日对话上限"
        desp = (
            f"用户 **{user.username}**（id `{user.id}`）今日对话已达 **{count}** 次，"
            f"超过上限 **{limit}** 次。\n\n后续对话将被拒绝（HTTP 429），到次日 0 点"
            f"（本地时区）重置。"
        )
    schedule_serverchan(title, desp, url=settings.SERVERCHAN_URL.get_secret_value())


def daily_chat_quota() -> Callable[..., Awaitable[None]]:
    """生成 Depends：对当前 user 做每日对话配额检查（普通用户限流 + Server酱通知）。

    用法：`@router.post(..., dependencies=[Depends(daily_chat_quota())])`
    """
    from app.core.auth import get_current_user

    async def _dep(
        request: Request,
        user: User = Depends(get_current_user),
        settings: Settings = Depends(get_settings),
    ) -> None:
        redis_client = getattr(request.app.state, "redis", None) or get_redis()

        async def on_event(kind: str, u: User, count: int, limit: int) -> None:
            await _serverchan_quota_event(kind, u, count, limit, settings=settings)

        await enforce_daily_chat_quota(
            redis_client, user=user, settings=settings, on_event=on_event
        )

    return _dep
