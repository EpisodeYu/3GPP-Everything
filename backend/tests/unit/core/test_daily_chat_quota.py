"""core/ratelimit.py 每日对话配额单测（普通用户 100/天 + 边界通知）。

用 in-memory Redis stub + 记录式 on_event，断言：
- 普通用户首次对话 count==1 触发 expire + on_event("first")
- admin 角色完全豁免（不计数 / 不通知）
- DAILY_CHAT_LIMIT<=0 关闭
- 超限抛 RateLimitedError(429)，且 "over" 仅在越界那一次触发
- 中间次数不触发任何 on_event
- 按 APP_TIMEZONE 本地日切：跨日计数重置
- 不同用户互不影响
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.core.errors import RateLimitedError
from app.core.ratelimit import DAILY_CHAT_BUCKET, enforce_daily_chat_quota
from app.db.models import User


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True


def _settings(limit: int = 3, tz: str = "Asia/Shanghai") -> Settings:
    return Settings(_env_file=None, DAILY_CHAT_LIMIT=limit, APP_TIMEZONE=tz)  # type: ignore[call-arg]


def _user(role: str = "user") -> User:
    return User(id=uuid.uuid4(), username=f"u_{role}", password_hash="x", role=role)


def _recorder() -> tuple[list[tuple[str, int]], object]:
    events: list[tuple[str, int]] = []

    async def on_event(kind: str, user: User, count: int, limit: int) -> None:
        events.append((kind, count))

    return events, on_event


async def test_first_call_counts_and_fires_first() -> None:
    client = FakeRedis()
    events, on_event = _recorder()
    count = await enforce_daily_chat_quota(
        client, user=_user(), settings=_settings(limit=3), on_event=on_event
    )
    assert count == 1
    assert len(client.expires) == 1  # 首次落桶挂 TTL
    assert DAILY_CHAT_BUCKET in next(iter(client.store))  # key 含本桶名
    assert events == [("first", 1)]


async def test_admin_is_exempt() -> None:
    client = FakeRedis()
    events, on_event = _recorder()
    result = await enforce_daily_chat_quota(
        client, user=_user(role="admin"), settings=_settings(limit=1), on_event=on_event
    )
    assert result is None
    assert client.store == {}  # 完全不计数
    assert events == []


async def test_disabled_when_limit_non_positive() -> None:
    client = FakeRedis()
    events, on_event = _recorder()
    result = await enforce_daily_chat_quota(
        client, user=_user(), settings=_settings(limit=0), on_event=on_event
    )
    assert result is None
    assert client.store == {}
    assert events == []


async def test_over_limit_raises_and_fires_over_once() -> None:
    client = FakeRedis()
    user = _user()
    events, on_event = _recorder()
    s = _settings(limit=3)

    # 前 3 次正常
    for _ in range(3):
        await enforce_daily_chat_quota(client, user=user, settings=s, on_event=on_event)

    # 第 4 次：越界 → 429 + 一条 "over"
    with pytest.raises(RateLimitedError) as ei:
        await enforce_daily_chat_quota(client, user=user, settings=s, on_event=on_event)
    assert ei.value.status_code == 429
    assert ei.value.details["bucket"] == DAILY_CHAT_BUCKET
    assert ei.value.details["limit"] == 3
    assert ei.value.details["count"] == 4

    # 第 5 次：仍抛，但不再重复推 "over"
    with pytest.raises(RateLimitedError):
        await enforce_daily_chat_quota(client, user=user, settings=s, on_event=on_event)

    assert events == [("first", 1), ("over", 4)]


async def test_no_event_on_middle_counts() -> None:
    client = FakeRedis()
    user = _user()
    events, on_event = _recorder()
    s = _settings(limit=5)
    for _ in range(4):  # count 1..4，均未越界
        await enforce_daily_chat_quota(client, user=user, settings=s, on_event=on_event)
    assert events == [("first", 1)]  # 仅首次，2/3/4 不通知


async def test_local_day_rollover_resets_counter() -> None:
    client = FakeRedis()
    user = _user()
    s = _settings(limit=3, tz="Asia/Shanghai")
    tz = ZoneInfo("Asia/Shanghai")

    # 今天 23:30（本地）打满
    today_2330 = datetime(2026, 5, 29, 23, 30, tzinfo=tz)
    for _ in range(3):
        await enforce_daily_chat_quota(client, user=user, settings=s, now=today_2330)
    with pytest.raises(RateLimitedError):
        await enforce_daily_chat_quota(client, user=user, settings=s, now=today_2330)

    # 次日 00:30（本地）→ 新桶，计数从 1 重新开始，不再抛
    next_0030 = datetime(2026, 5, 30, 0, 30, tzinfo=tz)
    count = await enforce_daily_chat_quota(client, user=user, settings=s, now=next_0030)
    assert count == 1


async def test_user_isolation() -> None:
    client = FakeRedis()
    s = _settings(limit=3)
    u1, u2 = _user(), _user()
    for _ in range(3):
        await enforce_daily_chat_quota(client, user=u1, settings=s)
    # u1 已满，u2 不受影响
    count = await enforce_daily_chat_quota(client, user=u2, settings=s)
    assert count == 1
