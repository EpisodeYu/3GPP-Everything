"""M4.10 集成测：`/api/v1/admin/*` stats / tasks / index_rebuild。

文档锚 04-backend-api.md §M4.10 验收清单：
- stats / tasks list / index_rebuild trigger 三条路径全覆盖
- upload-doc / crawl trigger 路由不存在（404，M4 主动推迟）
- RBAC：普通用户访问 → 403
- index_rebuild 触发 → audit_logs 写一行（admin.index_rebuild）
- task runner 桩：用 fake 把 status 推到 done，断言可轮询
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.models import ApiUsage, AuditLog, Task, User

from .test_auth import _bootstrap_admin, _login


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(client: Any) -> str:
    body = await _bootstrap_admin(client)
    assert body["role"] == "admin"
    pair = await _login(client, "admin1", "passw0rd!")
    return str(pair["access_token"])


async def _make_user(client: Any, admin_token: str, username: str = "u1") -> str:
    res = await client.post(
        "/api/v1/users",
        json={"username": username, "password": "passw0rd!", "role": "user"},
        headers=_h(admin_token),
    )
    assert res.status_code == 201, res.text
    pair = await _login(client, username, "passw0rd!")
    return str(pair["access_token"])


# === /admin/stats ===


async def test_stats_returns_counts_and_usage(app_and_state: Any, db_session: Any) -> None:
    """空库 → 各 count = admin 自身（用户 1 个）；注入 1 条 ApiUsage + 1 条 Task 验证聚合。"""
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)

        # 准备：写一行 ApiUsage（今天）+ 一条 done task
        admin_id = (
            await db_session.execute(select(User.id).where(User.username == "admin1"))
        ).scalar_one()
        db_session.add(
            ApiUsage(
                user_id=admin_id,
                day=date.today(),
                llm_input_tokens=100,
                llm_output_tokens=50,
                embedding_tokens=10,
                rerank_calls=2,
                web_search_calls=1,
                total_cost_usd=0.05,
            )
        )
        db_session.add(
            Task(kind="index_rebuild", payload={"spec_id": "x"}, status="done", progress=100)
        )
        await db_session.commit()

        r = await client.get("/api/v1/admin/stats", headers=_h(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["users"] >= 1
        assert body["sessions"] == 0
        assert body["messages"] == 0
        assert body["chunks"] == 0
        assert body["documents"] == 0
        assert body["tasks"] == {"done": 1}
        usage = body["api_usage_7d"]
        assert usage["llm_input_tokens"] == 100
        assert usage["llm_output_tokens"] == 50
        assert usage["embedding_tokens"] == 10
        assert usage["rerank_calls"] == 2
        assert usage["web_search_calls"] == 1
        assert usage["total_cost_usd"] == 0.05


async def test_stats_excludes_usage_older_than_7_days(app_and_state: Any, db_session: Any) -> None:
    """超过 7 天的 ApiUsage 不计入 api_usage_7d 聚合。"""
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)
        admin_id = (
            await db_session.execute(select(User.id).where(User.username == "admin1"))
        ).scalar_one()
        old = date.today() - timedelta(days=10)
        db_session.add(
            ApiUsage(
                user_id=admin_id,
                day=old,
                llm_input_tokens=999_999,
                llm_output_tokens=0,
                embedding_tokens=0,
                rerank_calls=0,
                web_search_calls=0,
                total_cost_usd=0.0,
            )
        )
        await db_session.commit()

        r = await client.get("/api/v1/admin/stats", headers=_h(token))
        assert r.status_code == 200
        assert r.json()["api_usage_7d"]["llm_input_tokens"] == 0


async def test_stats_requires_admin(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _admin_token(client)
        user_token = await _make_user(client, admin)
        r = await client.get("/api/v1/admin/stats", headers=_h(user_token))
        assert r.status_code == 403


async def test_stats_requires_auth(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/admin/stats")
        assert r.status_code == 401


# === /admin/tasks ===


async def test_list_tasks_returns_recent_first_with_status_filter(
    app_and_state: Any, db_session: Any
) -> None:
    """灌 3 条不同 status 的 task；不带 filter → 全部，按 created_at desc。"""
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)

        base = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)
        for i, status in enumerate(["queued", "running", "done"]):
            db_session.add(
                Task(
                    kind="index_rebuild",
                    payload={"i": i},
                    status=status,
                    progress=0,
                    created_at=base + timedelta(seconds=i),
                )
            )
        await db_session.commit()

        r = await client.get("/api/v1/admin/tasks", headers=_h(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 3
        statuses = [t["status"] for t in body["items"]]
        assert statuses == ["done", "running", "queued"]

        r2 = await client.get("/api/v1/admin/tasks?status=done", headers=_h(token))
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["total"] == 1
        assert body2["items"][0]["status"] == "done"


async def test_get_task_by_id_404_when_missing(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)
        bogus = str(uuid.uuid4())
        r = await client.get(f"/api/v1/admin/tasks/{bogus}", headers=_h(token))
        assert r.status_code == 404
        assert r.json()["code"] == "task_not_found"


async def test_tasks_routes_require_admin(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _admin_token(client)
        user_token = await _make_user(client, admin)
        for path in ["/api/v1/admin/tasks", f"/api/v1/admin/tasks/{uuid.uuid4()}"]:
            r = await client.get(path, headers=_h(user_token))
            assert r.status_code == 403


# === /admin/index/rebuild ===


def _stub_runner_factory(
    sessionmaker: Any, target_status: str = "done"
) -> tuple[Any, asyncio.Event]:
    """返回 (runner, done_event)：runner 把 task 推到 target_status 并 set event。

    使用集成测注入的 SQLite sessionmaker（不走 prod singleton）。
    """
    done_event = asyncio.Event()

    async def runner(task_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> None:
        from app.services.task_runner import _mark_finished, _mark_running

        async with sessionmaker() as db:
            await _mark_running(db, task_id)
        async with sessionmaker() as db:
            await _mark_finished(db, task_id, status=target_status, log_tail="ok")
        done_event.set()

    return runner, done_event


async def test_index_rebuild_creates_task_and_audit_then_runner_marks_done(
    app_and_state: Any, db_session: Any
) -> None:
    app, _, sm = app_and_state
    runner, done = _stub_runner_factory(sm, target_status="done")
    app.state.task_runner = runner

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)

        r = await client.post(
            "/api/v1/admin/index/rebuild",
            json={"spec_id": "23.501", "force": True},
            headers=_h(token),
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["kind"] == "index_rebuild"
        assert body["status"] == "queued"
        assert body["payload"] == {"spec_id": "23.501", "force": True}
        task_id = body["id"]

        # 等 runner 完成（≤ 2s 容忍 SQLite 写延迟）
        await asyncio.wait_for(done.wait(), timeout=2.0)

        # 轮询确认 done
        r2 = await client.get(f"/api/v1/admin/tasks/{task_id}", headers=_h(token))
        assert r2.status_code == 200
        assert r2.json()["status"] == "done"
        assert r2.json()["log_tail"] == "ok"

    # audit_logs 写了 admin.index_rebuild
    res = await db_session.execute(select(AuditLog).where(AuditLog.action == "admin.index_rebuild"))
    logs = res.scalars().all()
    assert len(logs) == 1
    assert logs[0].target_id == task_id
    assert logs[0].extra["payload"]["spec_id"] == "23.501"


async def test_index_rebuild_runner_failure_marks_failed(
    app_and_state: Any, db_session: Any
) -> None:
    app, _, sm = app_and_state
    runner, done = _stub_runner_factory(sm, target_status="failed")
    app.state.task_runner = runner

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)
        r = await client.post(
            "/api/v1/admin/index/rebuild",
            json={"spec_id": None, "force": False},
            headers=_h(token),
        )
        assert r.status_code == 202
        task_id = r.json()["id"]

        await asyncio.wait_for(done.wait(), timeout=2.0)

        r2 = await client.get(f"/api/v1/admin/tasks/{task_id}", headers=_h(token))
        assert r2.status_code == 200
        assert r2.json()["status"] == "failed"


async def test_index_rebuild_requires_admin(app_and_state: Any) -> None:
    app, _, _ = app_and_state

    # 不让真 runner 跑（防止 subprocess 起飞）
    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    app.state.task_runner = _noop

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _admin_token(client)
        user_token = await _make_user(client, admin)
        r = await client.post(
            "/api/v1/admin/index/rebuild",
            json={"spec_id": "x", "force": False},
            headers=_h(user_token),
        )
        assert r.status_code == 403


async def test_index_rebuild_ratelimited_after_5_calls(
    app_and_state: Any,
) -> None:
    """admin_crawl bucket = 5/d；6 次必触发 429。"""
    app, _, _ = app_and_state

    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    app.state.task_runner = _noop

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)
        for i in range(5):
            r = await client.post(
                "/api/v1/admin/index/rebuild",
                json={"spec_id": f"s-{i}", "force": False},
                headers=_h(token),
            )
            assert r.status_code == 202, (i, r.text)
        r6 = await client.post(
            "/api/v1/admin/index/rebuild",
            json={"spec_id": "s-x", "force": False},
            headers=_h(token),
        )
        assert r6.status_code == 429
        assert r6.json()["code"] == "rate_limited"


# === M4 推迟的路由：404 ===


async def test_upload_doc_route_not_registered(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)
        r = await client.post(
            "/api/v1/admin/upload-doc",
            json={"file_path": "x"},
            headers=_h(token),
        )
        assert r.status_code == 404


async def test_crawl_trigger_route_not_registered(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)
        r = await client.post(
            "/api/v1/admin/crawl",
            json={},
            headers=_h(token),
        )
        assert r.status_code == 404
