"""`app.services.alerts` 单测：阈值边界 + scheduler 启停（M7.4）。

文档锚点：`docs/03-development/06-evaluation-and-observability.md §9.2`。

覆盖：
- daily / monthly 阈值未超过 → 无 warning
- daily warn / critical 双档触发
- monthly 阈值触发
- ALERT_*_USD ≤ 0 → 视作 disabled
- AlertScheduler.start/shutdown 幂等
- ALERT_SCHEDULER_ENABLED=false → 不起 scheduler
- trigger_now 手动触发不依赖 cron
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import ApiUsage, Base, User
from app.services.alerts import AlertScheduler, run_alert_check_once


def _make_settings(**over: float | bool | int | str) -> Settings:
    defaults: dict[str, object] = dict(
        ALERT_DAILY_USD=5.0,
        ALERT_DAILY_USD_CRITICAL=10.0,
        ALERT_MONTHLY_USD=50.0,
        APP_TIMEZONE="Asia/Shanghai",
    )
    defaults.update(over)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type, call-arg]


@pytest_asyncio.fixture
async def sm_with_user() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    uid = uuid.uuid4()
    async with sm() as db:
        db.add(User(id=uid, username="alice", password_hash="x", role="user"))
        await db.commit()
    yield sm, uid
    await engine.dispose()


async def _seed_usage(
    sm: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    day: date,
    cost_usd: float,
) -> None:
    async with sm() as db:
        db.add(ApiUsage(user_id=user_id, day=day, total_cost_usd=cost_usd))
        await db.commit()


# === run_alert_check_once：阈值边界 ===


class TestAlertThresholds:
    async def test_no_warnings_under_thresholds(
        self,
        sm_with_user: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sm, uid = sm_with_user
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        await _seed_usage(sm, uid, yesterday, cost_usd=2.0)

        caplog.set_level(logging.WARNING, logger="app.services.alerts")
        async with sm() as db:
            res = await run_alert_check_once(db, settings=_make_settings(), today=today)

        assert not res.daily_warning
        assert not res.daily_critical
        assert not res.monthly_warning
        assert "cost_alert" not in caplog.text

    async def test_daily_warn_triggers_log(
        self,
        sm_with_user: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sm, uid = sm_with_user
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        await _seed_usage(sm, uid, yesterday, cost_usd=6.0)

        caplog.set_level(logging.WARNING, logger="app.services.alerts")
        async with sm() as db:
            res = await run_alert_check_once(db, settings=_make_settings(), today=today)

        assert res.daily_warning is True
        assert res.daily_critical is False
        assert "cost_alert.daily" in caplog.text
        assert "cost_alert.daily_critical" not in caplog.text

    async def test_daily_critical_triggers_critical_log_only(
        self,
        sm_with_user: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sm, uid = sm_with_user
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        await _seed_usage(sm, uid, yesterday, cost_usd=12.0)

        caplog.set_level(logging.WARNING, logger="app.services.alerts")
        async with sm() as db:
            res = await run_alert_check_once(db, settings=_make_settings(), today=today)

        assert res.daily_critical is True
        assert "cost_alert.daily_critical" in caplog.text
        # critical 时不再发普通 warn（避免 double-log）
        assert "cost_alert.daily " not in caplog.text and (
            caplog.text.count("cost_alert.daily") == 1
        )

    async def test_monthly_threshold_triggers_independently(
        self,
        sm_with_user: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sm, uid = sm_with_user
        today = datetime.now(UTC).date()
        # 把月累计撑过 50
        async with sm() as db:
            for d in range(1, today.day + 1):
                day = today.replace(day=d)
                db.add(ApiUsage(user_id=uid, day=day, total_cost_usd=5.0))
            await db.commit()

        caplog.set_level(logging.WARNING, logger="app.services.alerts")
        async with sm() as db:
            res = await run_alert_check_once(
                db, settings=_make_settings(ALERT_MONTHLY_USD=10.0), today=today
            )
        assert res.monthly_warning is True
        assert "cost_alert.monthly" in caplog.text

    async def test_zero_threshold_disabled(
        self,
        sm_with_user: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sm, uid = sm_with_user
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        await _seed_usage(sm, uid, yesterday, cost_usd=999.0)

        caplog.set_level(logging.WARNING, logger="app.services.alerts")
        async with sm() as db:
            res = await run_alert_check_once(
                db,
                settings=_make_settings(
                    ALERT_DAILY_USD=0,
                    ALERT_DAILY_USD_CRITICAL=0,
                    ALERT_MONTHLY_USD=0,
                ),
                today=today,
            )
        assert not res.daily_warning
        assert not res.daily_critical
        assert not res.monthly_warning
        assert "cost_alert" not in caplog.text


# === AlertScheduler ===


class TestAlertScheduler:
    async def test_start_idempotent(
        self,
    ) -> None:
        # AsyncIOScheduler.start() 需要 running event loop
        s = _make_settings()
        sched = AlertScheduler(settings=s)
        sched.start()
        first = sched._scheduler  # type: ignore[attr-defined]
        sched.start()  # 二次 start 应是 no-op
        second = sched._scheduler  # type: ignore[attr-defined]
        assert first is second
        sched.shutdown()
        # shutdown 后 _scheduler 清空；再 start 应能重建
        sched.start()
        assert sched._scheduler is not None  # type: ignore[attr-defined]
        sched.shutdown()

    async def test_disabled_does_not_start(self) -> None:
        s = _make_settings(ALERT_SCHEDULER_ENABLED=False)
        sched = AlertScheduler(settings=s)
        sched.start()
        assert sched._scheduler is None  # type: ignore[attr-defined]

    async def test_trigger_now_invokes_check(
        self,
        sm_with_user: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
    ) -> None:
        sm, _ = sm_with_user
        called: dict[str, int] = {"n": 0}

        async def fake_check(_db: AsyncSession):  # type: ignore[no-untyped-def]
            called["n"] += 1
            from app.services.alerts import AlertCheckResult

            return AlertCheckResult(
                day=date.today(),
                daily_cost_usd=0.0,
                monthly_cost_usd=0.0,
                daily_warning=False,
                daily_critical=False,
                monthly_warning=False,
            )

        sched = AlertScheduler(settings=_make_settings(), sessionmaker=sm, check_fn=fake_check)
        await sched.trigger_now()
        assert called["n"] == 1

    async def test_invalid_timezone_falls_back_to_utc(self) -> None:
        s = _make_settings(APP_TIMEZONE="Not/A_Real_TZ")
        sched = AlertScheduler(settings=s)
        sched.start()
        assert sched._scheduler is not None  # type: ignore[attr-defined]
        sched.shutdown()
