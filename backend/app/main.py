"""3GPP-Everything API entrypoint。

M4.6 起：
- 注册 `/api/v1/auth` + `/api/v1/users` 路由
- 统一 `AppError` exception handler → JSON 4xx/5xx
- CORS 允许 origins 来自 `settings.ALLOWED_ORIGINS`

M4.7：加上 `/api/v1/sessions`（含 SSE chat + cancel）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import auth as auth_routes
from app.api.v1 import chat as chat_routes
from app.api.v1 import checkpoint as checkpoint_routes
from app.api.v1 import sessions as sessions_routes
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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": app.version}

    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(users_routes.router, prefix="/api/v1")
    app.include_router(sessions_routes.router, prefix="/api/v1")
    app.include_router(chat_routes.router, prefix="/api/v1")
    app.include_router(checkpoint_routes.router, prefix="/api/v1")

    return app


app = create_app()
