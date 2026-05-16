"""rate_limit.CompositeLimiter 单测。

覆盖：
- 单 ctx 通过 + usage 累加
- RPM bound：burst > rpm 必须分摊到至少 period 秒
- TPM bound：单次 tokens > 当前剩余 → 等待
- mimo（仅 RPM）：tokens=0 不影响
- 单例 get/set/reset
"""

from __future__ import annotations

import asyncio

import pytest

from ingestion import rate_limit
from ingestion.rate_limit import (
    CompositeLimiter,
    get_mimo_limiter,
    get_voyage_limiter,
    reset_singletons,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_singletons()


def test_composite_single_ctx_increments_usage() -> None:
    async def _go() -> None:
        lim = CompositeLimiter(rpm=10, tpm=1000, name="t")
        async with lim.with_rate_limit(tokens=42):
            pass
        assert lim.usage.requests_made == 1
        assert lim.usage.tokens_used == 42

    asyncio.run(_go())


def test_rpm_burst_throttled() -> None:
    """RPM=6 / period=1s → 一次性发 12 个请求需要至少 1 个 period 才能跑完。

    AsyncLimiter 允许首个 burst 立即出 max_rate 个；剩余 6 个排队到下个 window。
    """

    async def _go() -> None:
        lim = CompositeLimiter(rpm=6, period_seconds=0.5, name="rpmtest")
        loop = asyncio.get_event_loop()
        t0 = loop.time()

        async def _one() -> None:
            async with lim.with_rate_limit():
                pass

        await asyncio.gather(*[_one() for _ in range(12)])
        elapsed = loop.time() - t0
        # 必须至少跨越一次 period（半个 window 也算）；不强求精确 0.5s 上界
        # 因 aiolimiter 漏桶 + 调度抖动可能稍长
        assert elapsed >= 0.4, f"expected >= 0.4s for 12 reqs at 6/0.5s, got {elapsed:.3f}"
        assert lim.usage.requests_made == 12

    asyncio.run(_go())


def test_tpm_weighted_acquire_blocks_when_exceeding() -> None:
    """TPM=100 / period=0.5s。先吞 80，再申请 80 → 第二次必须等待至少接近 0.5s。"""

    async def _go() -> None:
        lim = CompositeLimiter(rpm=100, tpm=100, period_seconds=0.5, name="tpmtest")
        loop = asyncio.get_event_loop()
        async with lim.with_rate_limit(tokens=80):
            pass
        t0 = loop.time()
        async with lim.with_rate_limit(tokens=80):
            pass
        elapsed = loop.time() - t0
        # 漏桶补满需 (80 - 20) / (100 / 0.5) ≈ 0.3s
        assert elapsed >= 0.2, f"expected weighted wait, got {elapsed:.3f}s"
        assert lim.usage.tokens_used == 160

    asyncio.run(_go())


def test_mimo_zero_tokens_only_rpm_counted() -> None:
    """tokens=0 时仅占 RPM；usage.tokens_used 不增。"""

    async def _go() -> None:
        lim = CompositeLimiter(rpm=100, tpm=10_000, name="mimotest")
        async with lim.with_rate_limit():
            pass
        async with lim.with_rate_limit(tokens=0):
            pass
        assert lim.usage.requests_made == 2
        assert lim.usage.tokens_used == 0

    asyncio.run(_go())


def test_singletons_default_reuse() -> None:
    a = get_voyage_limiter()
    b = get_voyage_limiter()
    assert a is b
    c = get_mimo_limiter()
    d = get_mimo_limiter()
    assert c is d
    assert a is not c


def test_singletons_reset() -> None:
    a = get_voyage_limiter()
    reset_singletons()
    b = get_voyage_limiter()
    assert a is not b


def test_singletons_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOYAGE_RPM", "42")
    monkeypatch.setenv("VOYAGE_TPM", "1234")
    reset_singletons()
    lim = get_voyage_limiter()
    assert lim.rpm == 42
    assert lim.tpm == 1234


def test_set_voyage_limiter_inject() -> None:
    custom = CompositeLimiter(rpm=5, tpm=50, name="custom")
    rate_limit.set_voyage_limiter(custom)
    assert get_voyage_limiter() is custom


def test_snapshot_usage_decoupled() -> None:
    async def _go() -> None:
        lim = CompositeLimiter(rpm=100, tpm=1000, name="snap")
        async with lim.with_rate_limit(tokens=10):
            pass
        snap = lim.snapshot_usage()
        async with lim.with_rate_limit(tokens=20):
            pass
        # snap 是快照，不会被后续 acquire 影响
        assert snap.tokens_used == 10
        assert lim.usage.tokens_used == 30

    asyncio.run(_go())


def test_invalid_rpm_raises() -> None:
    with pytest.raises(ValueError):
        CompositeLimiter(rpm=0, tpm=10, name="bad")
