"""3GPP-Everything API entrypoint。

M4.6 起：
- 注册 `/api/v1/auth` + `/api/v1/users` 路由
- 统一 `AppError` exception handler → JSON 4xx/5xx
- CORS 允许 origins 来自 `settings.ALLOWED_ORIGINS`

M4.7：加上 `/api/v1/sessions`（含 SSE chat + cancel）。
M4.9：加上 `/api/v1/docs` + `/api/v1/chunks` + `/api/v1/tools` + `/api/v1/favorites`
       + `/api/v1/notes` + `/api/v1/messages/{mid}/feedback`。
M4.10：加上 `/api/v1/admin` + `/health` + `/ready`。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.api import health as health_routes
from app.api.v1 import admin as admin_routes
from app.api.v1 import auth as auth_routes
from app.api.v1 import chat as chat_routes
from app.api.v1 import checkpoint as checkpoint_routes
from app.api.v1 import docs as docs_routes
from app.api.v1 import favorites as favorites_routes
from app.api.v1 import feedback as feedback_routes
from app.api.v1 import messages as messages_routes
from app.api.v1 import notes as notes_routes
from app.api.v1 import sessions as sessions_routes
from app.api.v1 import tools as tools_routes
from app.api.v1 import users as users_routes
from app.core.config import get_settings
from app.core.errors import AppError

log = logging.getLogger(__name__)


class _SkipLifespanInit(Exception):
    """conftest 设 disable_agent_init=True 时跳过 PG / 依赖初始化的内部信号。"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时：
    - 初始化 `app.state.in_flight_cancels`（F-1 cancel registry，run_id → asyncio.Event）
    - 若 DATABASE_URL 是 PostgreSQL：建 AsyncPostgresSaver checkpointer + 完整 LangGraph
      绑到 `app.state.agent_graph`，让 `aupdate_state` / fork / rollback / list_checkpoints
      真正持久化（M4.8 之前是 best-effort no-op）
    - 启动 M7.4 alerts scheduler（apscheduler 进程内 cron daily job，仅 log warning）

    关闭时：关掉 alerts scheduler、checkpointer 的连接池 + 释放 AgentDeps（dense / cache / llm）
    """
    app.state.in_flight_cancels = {}
    saver_ctx: Any = None
    alerts: Any = None
    try:
        # 测试钩子：conftest 设 `app.state.disable_agent_init = True` 防止 lifespan
        # 用真实 env 连 PG / Qdrant / LiteLLM
        if getattr(app.state, "disable_agent_init", False):
            raise _SkipLifespanInit()
        settings = get_settings()
        # 测试 / 没配 PG 的 dev 自然跳过；保留 `tgpp_agent` lazy 单例作 fallback
        if settings.DATABASE_URL.startswith("postgresql"):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            from app.agent.deps import AgentDeps
            from app.agent.graph import build_graph

            # psycopg 接受标准 URI；去掉 sqlalchemy 的 +asyncpg / +psycopg
            conn = settings.DATABASE_URL.replace("+asyncpg", "").replace("+psycopg", "")
            saver_ctx = AsyncPostgresSaver.from_conn_string(conn)
            saver = await saver_ctx.__aenter__()
            await saver.setup()
            deps = AgentDeps.from_env()
            app.state.agent_graph = build_graph(deps, checkpointer=saver)
            app.state._agent_deps = deps
            log.info("agent graph initialized with AsyncPostgresSaver checkpointer")
    except _SkipLifespanInit:
        log.debug("agent graph init skipped via disable_agent_init flag")
    except Exception as exc:
        # 上游不可达（PG / Qdrant / LiteLLM 任一）→ 不阻塞 API 起动；chat 路径会
        # fallback 到 lazy `tgpp_agent` 单例（其 first-touch 还是会按需触发）。
        log.warning("agent graph w/ checkpointer init skipped: %s", exc)

    # M7.4 alerts scheduler：进程内 daily cron job；任一异常都 swallow，不阻塞 API
    if not getattr(app.state, "disable_agent_init", False):
        try:
            from app.services.alerts import AlertScheduler

            alerts = AlertScheduler()
            alerts.start()
            app.state.alert_scheduler = alerts
        except Exception as exc:
            log.warning("alerts scheduler start failed: %s", exc, exc_info=False)

    try:
        yield
    finally:
        if alerts is not None:
            with contextlib.suppress(Exception):
                alerts.shutdown()
        live_deps: Any = getattr(app.state, "_agent_deps", None)
        if live_deps is not None:
            with contextlib.suppress(Exception):
                await live_deps.aclose()
        if saver_ctx is not None:
            with contextlib.suppress(Exception):
                await saver_ctx.__aexit__(None, None, None)


def create_app() -> FastAPI:
    settings = get_settings()
    # Swagger / ReDoc / openapi.json 仅在 dev 暴露；prod 关闭以缩小信息暴露面
    # （不对公网公开完整 API 形状）。
    docs_enabled = settings.APP_ENV == "dev"
    app = FastAPI(
        title="3GPP-Everything API",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    if settings.ALLOWED_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.ALLOWED_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(AppError)
    async def _app_error_handler(_req: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    app.include_router(health_routes.router)
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(users_routes.router, prefix="/api/v1")
    app.include_router(sessions_routes.router, prefix="/api/v1")
    app.include_router(messages_routes.router, prefix="/api/v1")
    app.include_router(chat_routes.router, prefix="/api/v1")
    app.include_router(checkpoint_routes.router, prefix="/api/v1")
    app.include_router(docs_routes.router, prefix="/api/v1")
    app.include_router(docs_routes.chunks_router, prefix="/api/v1")
    app.include_router(tools_routes.router, prefix="/api/v1")
    app.include_router(favorites_routes.router, prefix="/api/v1")
    app.include_router(notes_routes.router, prefix="/api/v1")
    app.include_router(feedback_routes.router, prefix="/api/v1")
    app.include_router(admin_routes.router, prefix="/api/v1")

    _autofill_openapi_metadata(app)
    return app


def _autofill_openapi_metadata(app: FastAPI) -> None:
    """OpenAPI 覆盖度兜底（M4.10 §M4.10 验收项）。

    - `summary` 为空时，把函数名 humanize（`list_users` → "List users"）写回去
    - `description` 为空时，先用函数 docstring 第一段，否则 fallback 到 summary
    保证 Swagger UI / openapi.json 每个 path operation 都有非空 summary + description；
    新增路由不必显式填这两项，自动补全（原已显式写的不动）。
    """
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.summary:
            route.summary = route.name.replace("_", " ").strip().capitalize() or route.path
        if not route.description:
            doc = (route.endpoint.__doc__ or "").strip()
            route.description = doc.split("\n\n", 1)[0] if doc else route.summary


app = create_app()
