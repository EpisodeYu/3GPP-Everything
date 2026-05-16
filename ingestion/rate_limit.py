"""全局异步速率限制器（M2 §4.8）。

跨所有 spec worker 共享的 token bucket：

| 资源 | 限速 | 瓶颈 |
|---|---|---|
| voyage-4-large | 3M TPM / 2000 RPM | TPM |
| mimo-v2.5 vision | 10M TPM / 100 RPM | RPM |

设计取舍：
- 用 `aiolimiter.AsyncLimiter` 单独维护 RPM / TPM；CompositeLimiter 同时 acquire 两者
- TPM 走 token-weighted `acquire(amount=tokens)`；RPM 走 `acquire(1)`
- 进程内单例：`get_voyage_limiter()` / `get_mimo_limiter()` 按 .env 懒加载
- 所有限速参数都从 env 读，方便 LiteLLM proxy 端调整或临时降速

接口契约：

```python
from ingestion.rate_limit import get_voyage_limiter, get_mimo_limiter

voyage = get_voyage_limiter()
async with voyage.with_rate_limit(tokens=12_000):
    ...  # 一次 voyage embed batch 调用，预扣 12k tokens

mimo = get_mimo_limiter()
async with mimo.with_rate_limit():           # token 默认 0：仅 RPM 计数
    ...  # 一次 vision 调用
```
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiolimiter import AsyncLimiter

DEFAULT_VOYAGE_TPM = 3_000_000
DEFAULT_VOYAGE_RPM = 2_000
DEFAULT_MIMO_TPM = 10_000_000
DEFAULT_MIMO_RPM = 100
_PERIOD_SECONDS = 60.0


@dataclass(slots=True)
class LimiterUsage:
    """累计统计，监控用（每 spec 完成后回填到 PipelineStats）。

    `tokens_used` / `requests_made`：自创建以来的累计值。
    `last_request_at`：最近一次 acquire 完成的 monotonic 时间戳（仅用于诊断）。
    """

    tokens_used: int = 0
    requests_made: int = 0
    last_request_at: float = 0.0


class CompositeLimiter:
    """RPM + 可选 TPM 复合限速器。

    - `tpm=None`：仅 RPM 限速（mimo 用，TPM 远不到瓶颈）
    - `tpm=N`：同时受 RPM + TPM 约束；`with_rate_limit(tokens=N)` 走 TPM 加权 acquire
    - aiolimiter 内部维护漏桶；多 worker 共享同一实例 = 全局速率上限
    """

    def __init__(
        self,
        *,
        rpm: int,
        tpm: int | None = None,
        period_seconds: float = _PERIOD_SECONDS,
        name: str = "limiter",
    ) -> None:
        if rpm <= 0:
            raise ValueError(f"{name}: rpm must be > 0, got {rpm}")
        self.name = name
        self.rpm = rpm
        self.tpm = tpm
        self._period = period_seconds
        # AsyncLimiter(max_rate, time_period)
        # 注意 aiolimiter 1.2 单次 acquire(amount) 的 amount 必须 ≤ max_rate；
        # 单次 voyage batch ≤ 64 chunks × ~500 tokens = 32k tokens，远 < 3M TPM 上限。
        self._rpm_limiter = AsyncLimiter(rpm, period_seconds)
        self._tpm_limiter: AsyncLimiter | None = (
            AsyncLimiter(tpm, period_seconds) if tpm is not None else None
        )
        self.usage = LimiterUsage()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def with_rate_limit(self, tokens: int = 0) -> AsyncIterator[None]:
        """异步 ctx：进入前先 acquire RPM + （可选）TPM 配额。

        `tokens` 为本次调用的预估 input tokens；< 0 视为 0；
        > tpm 上限会被截断并打 warning（理论上 batch=64 不会触发）。
        """
        await self._rpm_limiter.acquire()
        if self._tpm_limiter is not None and tokens > 0:
            amount = min(tokens, self.tpm or tokens)
            await self._tpm_limiter.acquire(amount)
        async with self._lock:
            self.usage.requests_made += 1
            self.usage.tokens_used += max(0, tokens)
            self.usage.last_request_at = asyncio.get_event_loop().time()
        # 漏桶 acquire 已落在进入时；ctx 退出无需归还（与 voyage/mimo 真实计费口径一致）
        yield

    def snapshot_usage(self) -> LimiterUsage:
        """复制一份 usage 供监控（CompositeLimiter 内部仍累加）。"""
        return LimiterUsage(
            tokens_used=self.usage.tokens_used,
            requests_made=self.usage.requests_made,
            last_request_at=self.usage.last_request_at,
        )

    def reset_usage(self) -> None:
        """清空 usage（pipeline 多次 run 之间隔离用）。"""
        self.usage = LimiterUsage()


# -------------------- 进程内单例 --------------------

_voyage_singleton: CompositeLimiter | None = None
_mimo_singleton: CompositeLimiter | None = None
_singleton_lock = asyncio.Lock()


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_voyage_limiter() -> CompositeLimiter:
    """全进程共享的 voyage 限速器（首次调用按 env 构造）。

    覆盖路径：测试可调 `set_voyage_limiter(...)` 注入替身。
    """
    global _voyage_singleton
    if _voyage_singleton is None:
        _voyage_singleton = CompositeLimiter(
            rpm=_env_int("VOYAGE_RPM", DEFAULT_VOYAGE_RPM),
            tpm=_env_int("VOYAGE_TPM", DEFAULT_VOYAGE_TPM),
            name="voyage",
        )
    return _voyage_singleton


def get_mimo_limiter() -> CompositeLimiter:
    """全进程共享的 mimo 限速器。RPM 为唯一硬瓶颈（100 RPM）。"""
    global _mimo_singleton
    if _mimo_singleton is None:
        _mimo_singleton = CompositeLimiter(
            rpm=_env_int("MIMO_RPM", DEFAULT_MIMO_RPM),
            tpm=_env_int("MIMO_TPM", DEFAULT_MIMO_TPM),
            name="mimo",
        )
    return _mimo_singleton


def set_voyage_limiter(limiter: CompositeLimiter | None) -> None:
    """测试钩子：注入或重置 voyage 单例。"""
    global _voyage_singleton
    _voyage_singleton = limiter


def set_mimo_limiter(limiter: CompositeLimiter | None) -> None:
    """测试钩子：注入或重置 mimo 单例。"""
    global _mimo_singleton
    _mimo_singleton = limiter


def reset_singletons() -> None:
    """清空两个单例（多个测试间互不污染）。"""
    set_voyage_limiter(None)
    set_mimo_limiter(None)


__all__ = [
    "DEFAULT_MIMO_RPM",
    "DEFAULT_MIMO_TPM",
    "DEFAULT_VOYAGE_RPM",
    "DEFAULT_VOYAGE_TPM",
    "CompositeLimiter",
    "LimiterUsage",
    "get_mimo_limiter",
    "get_voyage_limiter",
    "reset_singletons",
    "set_mimo_limiter",
    "set_voyage_limiter",
]
