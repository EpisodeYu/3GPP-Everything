"""成本告警 daily job（M7.4）。

文档锚点：`docs/03-development/06-evaluation-and-observability.md §9.2`。

Q2 决策（2026-05-19）：**仅 log warning**，不接 webhook / 邮件 / 推送通道；上线
监控需要时再扩。

设计：
- `apscheduler.AsyncIOScheduler` 进程内 cron job（默认每天 01:00 本地时区跑一次）。
- job 体：聚合昨日（避免读到当日还在累加的不完整数据）+ 月累计 → 与
  `Settings.ALERT_DAILY_USD/USD_CRITICAL/MONTHLY_USD` 比对 → log.warning。
- 失败容忍：聚合查询异常 → log + continue；scheduler 启动失败 → main.py lifespan
  仅 log 警告，不阻塞 API。

测试钩子：`run_alert_check_once(db)` 暴露纯函数，单测无需起 scheduler。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.base import get_sessionmaker
from app.services.usage import aggregate_month_cost_usd, aggregate_today_cost_usd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertCheckResult:
    """单次 daily check 输出（供测试断言）。"""

    day: date
    daily_cost_usd: float
    monthly_cost_usd: float
    daily_warning: bool
    daily_critical: bool
    monthly_warning: bool


def _resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("alerts: invalid APP_TIMEZONE=%r, falling back to UTC", name)
        return ZoneInfo("UTC")


async def run_alert_check_once(
    db: AsyncSession,
    *,
    settings: Settings | None = None,
    today: date | None = None,
) -> AlertCheckResult:
    """一次性聚合 + 阈值判断；同时 log warning 用于 §9.2 告警通道。

    `today` 缺省为 UTC 今天；alerts job 实际跑昨日（target = today - 1d）以避免
    读到当日仍在累加的不完整数据。caller 决定。
    """
    s = settings or get_settings()
    today = today or datetime.now(UTC).date()
    target = today - timedelta(days=1)
    daily_cost = await aggregate_today_cost_usd(db, day=target)
    monthly_cost = await aggregate_month_cost_usd(db, today=today)

    daily_warn = s.ALERT_DAILY_USD > 0 and daily_cost > s.ALERT_DAILY_USD
    daily_crit = s.ALERT_DAILY_USD_CRITICAL > 0 and daily_cost > s.ALERT_DAILY_USD_CRITICAL
    monthly_warn = s.ALERT_MONTHLY_USD > 0 and monthly_cost > s.ALERT_MONTHLY_USD

    if daily_crit:
        log.warning(
            "cost_alert.daily_critical day=%s cost_usd=%.4f threshold=%.2f",
            target.isoformat(),
            daily_cost,
            s.ALERT_DAILY_USD_CRITICAL,
        )
    elif daily_warn:
        log.warning(
            "cost_alert.daily day=%s cost_usd=%.4f threshold=%.2f",
            target.isoformat(),
            daily_cost,
            s.ALERT_DAILY_USD,
        )
    if monthly_warn:
        log.warning(
            "cost_alert.monthly month_through=%s cost_usd=%.4f threshold=%.2f",
            today.isoformat(),
            monthly_cost,
            s.ALERT_MONTHLY_USD,
        )

    return AlertCheckResult(
        day=target,
        daily_cost_usd=daily_cost,
        monthly_cost_usd=monthly_cost,
        daily_warning=daily_warn,
        daily_critical=daily_crit,
        monthly_warning=monthly_warn,
    )


# === scheduler ===


class AlertScheduler:
    """apscheduler `AsyncIOScheduler` 包装；main.py lifespan 启停。

    设计：
    - 单进程内单 scheduler；job id 固定，重复 start 幂等
    - 缺 sessionmaker（dev 没起 PG）时 scheduler 仍能 start，job 内捕获并 log
    """

    JOB_ID = "tgpp_alerts_daily"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        check_fn: Callable[[AsyncSession], Awaitable[AlertCheckResult]] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sm = sessionmaker
        self._scheduler: Any | None = None
        self._check_fn: Callable[[AsyncSession], Awaitable[AlertCheckResult]]
        self._check_fn = check_fn or self._default_check

    async def _default_check(self, db: AsyncSession) -> AlertCheckResult:
        return await run_alert_check_once(db, settings=self._settings)

    async def _job(self) -> None:
        sm = self._sm or get_sessionmaker()
        try:
            async with sm() as db:
                await self._check_fn(db)
        except Exception as exc:
            log.warning("alerts.job failed: %s", exc, exc_info=False)

    def start(self) -> None:
        if self._scheduler is not None:
            return
        if not self._settings.ALERT_SCHEDULER_ENABLED:
            log.info("alerts: scheduler disabled by ALERT_SCHEDULER_ENABLED=false")
            return
        try:
            from apscheduler.schedulers.asyncio import (  # type: ignore[import-untyped]
                AsyncIOScheduler,
            )
            from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover - 依赖缺失即 fail-open
            log.warning("alerts: apscheduler not installed, skipping daily job")
            return

        tz = _resolve_timezone(self._settings.APP_TIMEZONE)
        scheduler = AsyncIOScheduler(timezone=tz)
        scheduler.add_job(
            self._job,
            trigger=CronTrigger(
                hour=self._settings.ALERT_DAILY_AGGREGATE_HOUR,
                minute=0,
                timezone=tz,
            ),
            id=self.JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )
        scheduler.start()
        self._scheduler = scheduler
        log.info(
            "alerts: scheduler started tz=%s daily_at=%02d:00",
            tz.key,
            self._settings.ALERT_DAILY_AGGREGATE_HOUR,
        )

    def shutdown(self) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=False)
        except Exception as exc:
            log.warning("alerts: scheduler shutdown failed: %s", exc, exc_info=False)
        finally:
            self._scheduler = None

    async def trigger_now(self) -> None:
        """测试 / lifespan smoke 用：立刻跑一次 job 体（不经 cron）。"""
        await self._job()


__all__ = [
    "AlertCheckResult",
    "AlertScheduler",
    "run_alert_check_once",
]
