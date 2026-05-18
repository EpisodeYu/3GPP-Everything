"""异步任务 runner（M4.10 简化版）。

设计：
- M4.10：用 `asyncio.create_task` 包 ingestion CLI 子进程；进程内单点队列。
- M8 上线前换 Redis Streams + 独立 worker（详见
  `docs/03-development/04-backend-api.md §9.2`）。

测试注入：
- 路由侧从 `request.app.state.task_runner` 读 callable，缺省走 `default_task_runner`
- 单测/集成测灌入同步桩（立即把 Task 状态推到 done）以避开 subprocess + 真实 PG

约束：
- runner 自己开新的 DB session（`asyncio.create_task` 已脱离原 request scope）
- 进程崩溃 / 重启 → in-flight 任务丢失（M4 限制；M8 worker 解决）
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import get_sessionmaker
from app.db.models import Task

log = logging.getLogger(__name__)

TaskRunner = Callable[[uuid.UUID, str, dict[str, Any]], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


def build_index_rebuild_argv(payload: dict[str, Any]) -> list[str]:
    """根据 payload 构造 `python -m ingestion.cli` 命令行（M4.10 简化版）。

    spec_id=None → 全量；spec_id 提供 → 单 spec 重建。force=True → 透传 --purge-first
    （ingestion.indexer.runner 现有 flag）。
    """
    spec_id = payload.get("spec_id")
    force = bool(payload.get("force"))
    argv: list[str] = [sys.executable, "-m", "ingestion.cli", "pipeline-hf"]
    if spec_id:
        argv += ["--spec-id", str(spec_id)]
    if force:
        argv += ["--purge-first"]
    return argv


async def _run_subprocess(argv: list[str]) -> tuple[int, str]:
    """跑 subprocess，捕获 tail 日志（最多 8KB）；返回 (returncode, log_tail)。"""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    chunks: list[bytes] = []
    total = 0
    cap = 8192
    if proc.stdout is not None:
        async for raw in proc.stdout:
            chunks.append(raw)
            total += len(raw)
            if total > cap * 2:
                # 防内存膨胀：滚动截断
                chunks = chunks[-200:]
                total = sum(len(c) for c in chunks)
    rc = await proc.wait()
    blob = b"".join(chunks)[-cap:]
    return rc, blob.decode("utf-8", errors="replace")


async def default_task_runner(
    task_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """默认 runner：把 task 标 running → 跑 subprocess → 写 done/failed。

    所有 DB 写都在新 session 内（脱离 request scope）。kind=index_rebuild 时跑
    `ingestion.cli pipeline-hf`；其它 kind 当前直接标 failed（M4 不实现 crawl）。
    """
    sm = sessionmaker or get_sessionmaker()
    async with sm() as db:
        await _mark_running(db, task_id)
    try:
        if kind == "index_rebuild":
            argv = build_index_rebuild_argv(payload)
            log.info("task.run task_id=%s argv=%s", task_id, shlex.join(argv))
            rc, log_tail = await _run_subprocess(argv)
            status = "done" if rc == 0 else "failed"
            async with sm() as db:
                await _mark_finished(db, task_id, status=status, log_tail=log_tail)
        else:
            async with sm() as db:
                await _mark_finished(
                    db,
                    task_id,
                    status="failed",
                    log_tail=f"unsupported task kind: {kind}",
                )
    except Exception as exc:
        log.exception("task.crashed task_id=%s", task_id)
        async with sm() as db:
            await _mark_finished(db, task_id, status="failed", log_tail=repr(exc)[-2048:])


async def _mark_running(db: AsyncSession, task_id: uuid.UUID) -> None:
    res = await db.execute(select(Task).where(Task.id == task_id))
    t = res.scalar_one_or_none()
    if t is None:
        return
    t.status = "running"
    t.started_at = _now()
    await db.commit()


async def _mark_finished(
    db: AsyncSession,
    task_id: uuid.UUID,
    *,
    status: str,
    log_tail: str = "",
) -> None:
    res = await db.execute(select(Task).where(Task.id == task_id))
    t = res.scalar_one_or_none()
    if t is None:
        return
    t.status = status
    t.finished_at = _now()
    t.progress = 100 if status == "done" else t.progress
    if log_tail:
        t.log_tail = log_tail
    await db.commit()


def schedule_task(
    runner: TaskRunner,
    *,
    task_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
) -> asyncio.Task[None]:
    """fire-and-forget：拿到 asyncio task 用于测试 await。"""
    coro = runner(task_id, kind, payload)
    # 类型上 Awaitable 不一定是 Coroutine；asyncio.create_task 需要 coroutine 才能 schedule。
    # 这里用 ensure_future 接收 Awaitable 后再保证返回 Task。
    return asyncio.ensure_future(coro)
