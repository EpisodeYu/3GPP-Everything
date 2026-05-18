"""M4.10 集成测：OpenAPI 覆盖度。

文档锚 04-backend-api.md §M4.10：
- 所有路由有 `summary` + `description`（自动 fill 也算）
- `/openapi.json` 可访问

不验证 schema 业务字段（那是各个路由集成测的事），只验证 path operation 元数据完整。
"""

from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient


async def test_openapi_json_returns_200(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert "paths" in spec
        assert spec["paths"], "openapi paths must not be empty"


async def test_every_path_operation_has_summary_and_description(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/openapi.json")
        spec = r.json()

    missing: list[str] = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method.lower() not in {"get", "post", "patch", "delete", "put", "options", "head"}:
                continue
            summary = (op.get("summary") or "").strip()
            description = (op.get("description") or "").strip()
            if not summary or not description:
                missing.append(
                    f"{method.upper()} {path}: summary={summary!r} description={description!r}"
                )
    assert not missing, "OpenAPI metadata gaps:\n" + "\n".join(missing)


async def test_admin_routes_present_in_openapi(app_and_state: Any) -> None:
    """M4.10 admin 路由全部出现在 openapi paths；推迟的 upload-doc / crawl 缺席。"""
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        spec = (await client.get("/openapi.json")).json()
    paths = set(spec["paths"].keys())
    assert "/api/v1/admin/stats" in paths
    assert "/api/v1/admin/tasks" in paths
    assert "/api/v1/admin/tasks/{tid}" in paths
    assert "/api/v1/admin/index/rebuild" in paths
    assert "/api/v1/admin/upload-doc" not in paths
    assert "/api/v1/admin/crawl" not in paths


async def test_health_and_ready_in_openapi(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        spec = (await client.get("/openapi.json")).json()
    paths = set(spec["paths"].keys())
    assert "/health" in paths
    assert "/ready" in paths


async def test_swagger_ui_accessible(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/docs")
        assert r.status_code == 200
        assert "swagger" in r.text.lower()
