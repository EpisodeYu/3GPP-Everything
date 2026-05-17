"""结构化日志（structlog）配置。

- prod：JSON 一行一条
- dev：pretty console
- 字段：`trace_id` 由调用方注入（与 Langfuse trace 对齐）；不在此层埋点

`configure_logging()` 应在进程入口（main.py / alembic env.py / pytest conftest）调用一次。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(*, level: str | int = "INFO", json_mode: bool | None = None) -> None:
    """初始化 structlog。

    `json_mode=None` 时按 APP_ENV 决定（prod -> JSON / dev -> console）；可显式覆盖。
    """
    if json_mode is None:
        from .config import get_settings

        json_mode = get_settings().APP_ENV == "prod"

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_mode:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_level(level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 把 stdlib logging 也吸过来（uvicorn / httpx / sqlalchemy 走 stdlib）
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=_resolve_level(level),
        force=True,
    )


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return logging.getLevelName(level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """统一 logger 工厂；business code 用 `get_logger(__name__)`。"""
    return structlog.get_logger(name)
