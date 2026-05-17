"""Retrieval 结果缓存（Redis JSON, TTL 1h）。

key 格式 `{prefix}:{namespace}:{sha256(payload)}`，payload 由 caller 决定（典型
= `query + filter`）；value = JSON serialized 任意结构。

设计选择：
- 不缓存原始 RetrievedChunk 对象 → 缓存上层语义结果（dense top-50 / rerank top-5 等），
  caller 负责序列化/反序列化为 RetrievedChunk
- Redis 客户端用 redis.asyncio；测试用 fakeredis 或者直接注入 dict-backed stub
- 失败优雅降级：cache miss / Redis 不可达 → 直接返回 None，不阻塞主路径
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)


class RetrievalCache:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client  # 注入式：aioredis.Redis 或 stub
        self._owns_client = client is None

    async def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = aioredis.from_url(self._settings.REDIS_URL, decode_responses=True)
        return self._client

    @staticmethod
    def _hash_payload(payload: Any) -> str:
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def make_key(self, namespace: str, payload: Any) -> str:
        return f"{self._settings.CACHE_KEY_PREFIX}:{namespace}:{self._hash_payload(payload)}"

    async def get(self, namespace: str, payload: Any) -> Any | None:
        key = self.make_key(namespace, payload)
        try:
            cli = await self._ensure_client()
            raw = await cli.get(key)
        except Exception as exc:
            log.warning("cache.get failed namespace=%s err=%s", namespace, exc)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            log.warning("cache.get malformed value at %s, dropping", key)
            return None

    async def set(
        self,
        namespace: str,
        payload: Any,
        value: Any,
        *,
        ttl_s: int | None = None,
    ) -> None:
        key = self.make_key(namespace, payload)
        ttl = ttl_s if ttl_s is not None else self._settings.RETRIEVAL_CACHE_TTL_S
        try:
            cli = await self._ensure_client()
            await cli.set(key, json.dumps(value, default=str, ensure_ascii=False), ex=int(ttl))
        except Exception as exc:
            log.warning("cache.set failed namespace=%s err=%s", namespace, exc)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            with _suppress():
                await self._client.aclose()


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> bool:
        return True
