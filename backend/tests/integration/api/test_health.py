"""M4.10 集成测：`/health` (liveness) + `/ready` (readiness with deps probe)。

口径：
- /health 永远 200（不检依赖）
- /ready 任一依赖 fail → 503，body 列每个依赖的 ok/error 状态
- 全部 ok → 200 + ok=True
"""

from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient

from app.api.health import ReadyProbe


async def test_health_always_ok(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body


async def _ok(_settings: Any) -> None:
    return None


async def _fail(_settings: Any) -> None:
    raise RuntimeError("boom")


async def _hang(_settings: Any) -> None:
    import asyncio

    await asyncio.sleep(10)


async def test_ready_all_ok_returns_200(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    app.state.ready_probes = [
        ReadyProbe("postgres", _ok),
        ReadyProbe("qdrant", _ok),
        ReadyProbe("redis", _ok),
        ReadyProbe("litellm", _ok),
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/ready")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        names = {c["name"] for c in body["checks"]}
        assert names == {"postgres", "qdrant", "redis", "litellm"}
        assert all(c["ok"] for c in body["checks"])


async def test_ready_returns_503_when_one_dep_down(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    app.state.ready_probes = [
        ReadyProbe("postgres", _ok),
        ReadyProbe("qdrant", _fail),  # 模拟 qdrant 挂
        ReadyProbe("redis", _ok),
        ReadyProbe("litellm", _ok),
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/ready")
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["ok"] is False
        bad = next(c for c in body["checks"] if c["name"] == "qdrant")
        assert bad["ok"] is False
        assert "boom" in bad["error"]
        # 其它 ok
        for name in ("postgres", "redis", "litellm"):
            good = next(c for c in body["checks"] if c["name"] == name)
            assert good["ok"] is True


async def test_ready_returns_503_on_probe_timeout(app_and_state: Any) -> None:
    """单个 probe 超时 → 不会拖垮整体响应；body 里那项标 timeout。"""
    app, _, _ = app_and_state
    app.state.ready_probes = [
        ReadyProbe("postgres", _ok),
        ReadyProbe("redis", _hang, timeout_s=0.05),
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["ok"] is False
        bad = next(c for c in body["checks"] if c["name"] == "redis")
        assert bad["ok"] is False
        assert bad["error"] == "timeout"
