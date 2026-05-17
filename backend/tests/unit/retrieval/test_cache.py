"""RetrievalCache 用注入式 stub redis 验证 set/get 与失败降级。"""

from __future__ import annotations

import json
from typing import Any

from app.core.config import Settings
from app.retrieval.cache import RetrievalCache


class _StubRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.fail = fail

    async def get(self, key: str) -> Any:
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value

    async def aclose(self) -> None:
        pass


def _settings() -> Settings:
    return Settings(_env_file=None, CACHE_KEY_PREFIX="tgpp:cache", RETRIEVAL_CACHE_TTL_S=10)  # type: ignore[call-arg]


async def test_roundtrip() -> None:
    redis = _StubRedis()
    cache = RetrievalCache(settings=_settings(), client=redis)
    payload = {"q": "PDU Session", "filter": ["23.501"]}
    assert await cache.get("retrieve", payload) is None
    await cache.set("retrieve", payload, {"chunks": ["c1", "c2"]})
    got = await cache.get("retrieve", payload)
    assert got == {"chunks": ["c1", "c2"]}
    # 同 key 不同 namespace 互不干扰
    assert await cache.get("rerank", payload) is None


async def test_key_includes_namespace_and_prefix() -> None:
    redis = _StubRedis()
    cache = RetrievalCache(settings=_settings(), client=redis)
    key = cache.make_key("retrieve", {"x": 1})
    assert key.startswith("tgpp:cache:retrieve:")
    # determinism
    assert key == cache.make_key("retrieve", {"x": 1})
    # 顺序不影响
    assert cache.make_key("retrieve", {"a": 1, "b": 2}) == cache.make_key(
        "retrieve", {"b": 2, "a": 1}
    )


async def test_get_failure_returns_none_silently() -> None:
    cache = RetrievalCache(settings=_settings(), client=_StubRedis(fail=True))
    assert await cache.get("retrieve", {"x": 1}) is None


async def test_set_failure_does_not_raise() -> None:
    cache = RetrievalCache(settings=_settings(), client=_StubRedis(fail=True))
    await cache.set("retrieve", {"x": 1}, {"y": 2})  # 不抛


async def test_value_serialization_uses_default_for_non_json() -> None:
    redis = _StubRedis()
    cache = RetrievalCache(settings=_settings(), client=redis)
    from datetime import datetime

    ts = datetime(2026, 5, 17, 12, 0, 0)
    await cache.set("x", {"k": 1}, {"ts": ts})
    raw = next(iter(redis.store.values()))
    obj = json.loads(raw)
    assert "ts" in obj  # datetime 通过 default=str 序列化为字符串
