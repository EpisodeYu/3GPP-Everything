"""core/ratelimit.py 单测：定窗 INCR + EXPIRE 算法。

用一个 in-memory Redis stub，断言：
- 首次 INCR 触发 expire
- 第 N+1 次（超 limit）抛 RateLimitedError
- 窗口切换后计数从 1 重新开始
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.errors import RateLimitedError
from app.core.ratelimit import BUCKETS, _client_ip, consume


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


@pytest.mark.parametrize("bucket", list(BUCKETS.keys()))
async def test_first_consume_sets_expire(bucket: str) -> None:
    client = FakeRedis()
    uid = uuid.uuid4()
    await consume(client, user_id=uid, bucket=bucket, now=1000)
    assert len(client.expires) == 1
    assert next(iter(client.expires.values())) == BUCKETS[bucket].period_s


async def test_over_limit_raises_rate_limited() -> None:
    client = FakeRedis()
    uid = uuid.uuid4()
    spec = BUCKETS["chat"]
    # 跑满 limit 不抛
    for _ in range(spec.limit):
        await consume(client, user_id=uid, bucket="chat", now=1000)
    # 再一次 → 429
    with pytest.raises(RateLimitedError) as ei:
        await consume(client, user_id=uid, bucket="chat", now=1000)
    assert ei.value.status_code == 429
    assert ei.value.details["bucket"] == "chat"


async def test_window_rollover_resets_counter() -> None:
    client = FakeRedis()
    uid = uuid.uuid4()
    spec = BUCKETS["chat"]
    # 用满 window 1
    for _ in range(spec.limit):
        await consume(client, user_id=uid, bucket="chat", now=1000)
    # 跳到下一个 period（now 跳 period_s 秒）
    next_now = 1000 + spec.period_s + 1
    # 不抛，且计数重新从 1 开始
    count = await consume(client, user_id=uid, bucket="chat", now=next_now)
    assert count == 1


async def test_unknown_bucket_raises_key_error() -> None:
    with pytest.raises(KeyError):
        await consume(FakeRedis(), user_id=uuid.uuid4(), bucket="no_such_bucket", now=0)


async def test_user_isolation() -> None:
    client = FakeRedis()
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    spec = BUCKETS["chat"]
    for _ in range(spec.limit):
        await consume(client, user_id=u1, bucket="chat", now=1000)
    # u2 不受 u1 已用满影响
    c = await consume(client, user_id=u2, bucket="chat", now=1000)
    assert c == 1


async def test_consume_returns_count() -> None:
    client = FakeRedis()
    uid = uuid.uuid4()
    c1: Any = await consume(client, user_id=uid, bucket="chat", now=1000)
    c2 = await consume(client, user_id=uid, bucket="chat", now=1000)
    assert c1 == 1 and c2 == 2


# === _client_ip：登录限流用的 IP 解析（X-Real-IP 优先，不信任 XFF leftmost）===


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, headers: dict[str, str], client_host: str | None) -> None:
        self.headers = headers
        self.client = _FakeClient(client_host) if client_host is not None else None


def test_client_ip_prefers_x_real_ip() -> None:
    req = _FakeRequest({"x-real-ip": "203.0.113.7"}, client_host="10.0.0.1")
    assert _client_ip(req) == "203.0.113.7"  # type: ignore[arg-type]


def test_client_ip_strips_whitespace() -> None:
    req = _FakeRequest({"x-real-ip": "  203.0.113.7 "}, client_host=None)
    assert _client_ip(req) == "203.0.113.7"  # type: ignore[arg-type]


def test_client_ip_falls_back_to_peer_host() -> None:
    req = _FakeRequest({}, client_host="10.0.0.5")
    assert _client_ip(req) == "10.0.0.5"  # type: ignore[arg-type]


def test_client_ip_unknown_when_no_source() -> None:
    req = _FakeRequest({}, client_host=None)
    assert _client_ip(req) == "unknown"  # type: ignore[arg-type]
