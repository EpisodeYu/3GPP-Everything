"""`/health` (liveness) + `/ready` (readiness with deps probe)。

文档锚点：`docs/03-development/04-backend-api.md §2 Health / §M4.10`。

设计：
- `/health`：只判进程活着，永远 200（K8s liveness probe 标准做法）
- `/ready`：检 PG / Qdrant / Redis / LiteLLM 四个依赖；任一不可达 → 503
  + body 列出每个依赖的 `ok`/`error` 状态，便于 SRE 看出是哪个挂

每个 probe 有独立超时（默认 3s），不会因为某个上游慢拖垮整体响应。

测试注入：
- `request.app.state.ready_probes` = list[ReadyProbe]：测试自定义所有 probe 行为
- 缺省走 `default_probes()`：真实 PG/Qdrant/Redis/LiteLLM 检测
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx
from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

ProbeFn = Callable[[Settings], Awaitable[None]]


@dataclass(frozen=True)
class ReadyProbe:
    """一个 readiness 检测项；执行成功视作 ok=True，抛任何异常视作 ok=False+error message。"""

    name: str
    fn: ProbeFn
    timeout_s: float = 3.0


# === 默认 probe 实现 ===


async def _probe_postgres(settings: Settings) -> None:
    from app.db.base import get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _probe_qdrant(settings: Settings) -> None:
    from qdrant_client import AsyncQdrantClient

    api_key = settings.QDRANT_API_KEY.get_secret_value() or None
    cli = AsyncQdrantClient(url=settings.QDRANT_URL, api_key=api_key, timeout=3)
    try:
        await cli.get_collections()
    finally:
        await cli.close()


async def _probe_redis(settings: Settings) -> None:
    import redis.asyncio as aioredis

    cli = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        # ping 在 redis-py 5.x 类型签名是 Awaitable[bool] | bool 联合；await cast 收敛
        await cast(Awaitable[Any], cli.ping())
    finally:
        await cli.aclose()


async def _probe_litellm(settings: Settings) -> None:
    """LiteLLM proxy 自带 `/health/liveliness`（不需鉴权，仅判进程）。"""
    base = settings.LITELLM_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}/health/liveliness"
    async with httpx.AsyncClient(timeout=3.0) as cli:
        resp = await cli.get(url)
        resp.raise_for_status()


def default_probes() -> list[ReadyProbe]:
    return [
        ReadyProbe("postgres", _probe_postgres),
        ReadyProbe("qdrant", _probe_qdrant),
        ReadyProbe("redis", _probe_redis),
        ReadyProbe("litellm", _probe_litellm),
    ]


# === routes ===


@router.get(
    "/health",
    summary="Liveness probe",
)
def health(request: Request) -> dict[str, str]:
    """进程存活探针：永远返回 ok（不查依赖，K8s liveness 用）。

    带上 `version` 字段方便上线后排查多副本是否同版本（与 FastAPI app.version 同步）。
    """
    return {"status": "ok", "version": request.app.version}


@router.get(
    "/ready",
    summary="Readiness probe（带 PG/Qdrant/Redis/LiteLLM 依赖检测）",
)
async def ready(request: Request, response: Response) -> dict[str, Any]:
    """检测每个依赖；任一不通 → 503，body 列每个依赖 ok/error 详情。"""
    settings = get_settings()
    probes: list[ReadyProbe] = getattr(request.app.state, "ready_probes", None) or default_probes()

    async def _run_one(p: ReadyProbe) -> dict[str, Any]:
        try:
            await asyncio.wait_for(p.fn(settings), timeout=p.timeout_s)
            return {"name": p.name, "ok": True}
        except TimeoutError:
            return {"name": p.name, "ok": False, "error": "timeout"}
        except Exception as exc:
            return {"name": p.name, "ok": False, "error": _short_err(exc)}

    results = await asyncio.gather(*(_run_one(p) for p in probes))
    all_ok = all(r["ok"] for r in results)
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ok": all_ok, "checks": results}


def _short_err(exc: BaseException) -> str:
    s = repr(exc)
    return s if len(s) <= 256 else s[:253] + "..."
