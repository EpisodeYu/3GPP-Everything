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
from app.api.v1 import notes as notes_routes
from app.api.v1 import sessions as sessions_routes
from app.api.v1 import tools as tools_routes
from app.api.v1 import users as users_routes
from app.core.config import get_settings
from app.core.errors import AppError


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="3GPP-Everything API", version="0.2.0")

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
