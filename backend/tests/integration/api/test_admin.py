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

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.models import ApiUsage, AuditLog, Feedback, Message, Task, User

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


async def test_stats_reflects_real_usage_hook_writes(app_and_state: Any, db_session: Any) -> None:
    """M7.4 写入链路：直接调 4 路 record_*_usage（绑当前 SQLite sm + 真实 user_id）→
    /admin/stats `api_usage_7d` 字段聚合到一致数字。

    单测层已覆盖 `_upsert_api_usage`；本集成测验证：
      - sessionmaker_override 注入路径正确
      - 多次累加后 /admin/stats 暴露的 7d 聚合 = sum(usage)
    """
    from app.services import usage as usage_mod

    app, _, sm = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)

        admin_id = (
            await db_session.execute(select(User.id).where(User.username == "admin1"))
        ).scalar_one()

        usage_mod.set_sessionmaker_override(sm)
        try:
            # 模拟一次 chat（mimo-v2.5 1000+500 token）+ 一次 embed（voyage 5000 token）
            # + 一次 rerank + 一次 web_search（tavily）
            await usage_mod.record_llm_usage(
                model="mimo-v2.5",
                prompt_tokens=1000,
                completion_tokens=500,
                user_id=admin_id,
            )
            await usage_mod.record_embedding_usage(
                model="voyage-4-large",
                tokens=5000,
                user_id=admin_id,
            )
            await usage_mod.record_rerank_usage(
                model="rerank-2.5",
                query_tokens=20,
                doc_tokens=2000,
                n_docs=10,
                user_id=admin_id,
            )
            await usage_mod.record_web_search_usage(
                provider="tavily-search",
                calls=2,
                user_id=admin_id,
            )
        finally:
            usage_mod.set_sessionmaker_override(None)

        r = await client.get("/api/v1/admin/stats", headers=_h(token))
        assert r.status_code == 200, r.text
        u = r.json()["api_usage_7d"]
        assert u["llm_input_tokens"] == 1000
        assert u["llm_output_tokens"] == 500
        assert u["embedding_tokens"] == 5000
        assert u["rerank_calls"] == 1
        assert u["web_search_calls"] == 2
        # mimo-v2.5: 0.4/M × 1000 + 2.0/M × 500 = 4e-4 + 1e-3 = 1.4e-3
        # voyage: 0 (free tier), rerank: 0 (free tier), tavily: 2 × 0.01 = 0.02
        assert u["total_cost_usd"] == pytest.approx(0.0014 + 0.02, rel=1e-6)


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


# === /admin/feedback ===


async def test_feedback_list_stats_and_filter(app_and_state: Any, db_session: Any) -> None:
    """聚合计数取全量、列表带消息预览/反馈者/会话、thumb filter 只过列表不动计数。"""
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)
        admin_id = (
            await db_session.execute(select(User.id).where(User.username == "admin1"))
        ).scalar_one()

        r = await client.post("/api/v1/sessions", json={"title": "s"}, headers=_h(token))
        sid = r.json()["id"]
        m_up = Message(
            session_id=uuid.UUID(sid), role="assistant", content="good answer about NR", status="ok"
        )
        m_down = Message(
            session_id=uuid.UUID(sid), role="assistant", content="bad answer", status="ok"
        )
        db_session.add_all([m_up, m_down])
        await db_session.flush()
        db_session.add_all(
            [
                Feedback(user_id=admin_id, message_id=m_up.id, thumb=1, reason=None),
                Feedback(user_id=admin_id, message_id=m_down.id, thumb=-1, reason="not helpful"),
            ]
        )
        await db_session.commit()

        r = await client.get("/api/v1/admin/feedback", headers=_h(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stats"] == {"up": 1, "down": 1, "total": 2}
        assert body["total"] == 2
        assert len(body["items"]) == 2
        down = next(it for it in body["items"] if it["thumb"] == -1)
        assert down["reason"] == "not helpful"
        assert down["username"] == "admin1"
        assert down["message_preview"] == "bad answer"
        assert down["session_id"] == sid

        # thumb filter 只过列表，stats 仍全量
        r = await client.get("/api/v1/admin/feedback?thumb=-1", headers=_h(token))
        b2 = r.json()
        assert b2["total"] == 1
        assert all(it["thumb"] == -1 for it in b2["items"])
        assert b2["stats"] == {"up": 1, "down": 1, "total": 2}


async def test_feedback_requires_admin(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_token = await _admin_token(client)
        user_token = await _make_user(client, admin_token, "u1")
        r = await client.get("/api/v1/admin/feedback", headers=_h(user_token))
        assert r.status_code == 403


# === /admin/sessions/{sid} ===


async def test_admin_can_read_any_users_session(app_and_state: Any, db_session: Any) -> None:
    """admin 读普通用户 u1 的整个会话：返回 title/owner + 全部消息（含引用）。"""
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_token = await _admin_token(client)
        user_token = await _make_user(client, admin_token, "u1")

        r = await client.post(
            "/api/v1/sessions", json={"title": "u1 的会话"}, headers=_h(user_token)
        )
        sid = r.json()["id"]
        db_session.add_all(
            [
                Message(
                    session_id=uuid.UUID(sid), role="user", content="什么是 PDCP", status="ok"
                ),
                Message(
                    session_id=uuid.UUID(sid), role="assistant", content="PDCP 是…", status="ok"
                ),
            ]
        )
        await db_session.commit()

        r = await client.get(f"/api/v1/admin/sessions/{sid}", headers=_h(admin_token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["title"] == "u1 的会话"
        assert body["username"] == "u1"
        assert len(body["messages"]) == 2
        roles = {m["role"] for m in body["messages"]}
        assert roles == {"user", "assistant"}
        user_msg = next(m for m in body["messages"] if m["role"] == "user")
        assert user_msg["content"] == "什么是 PDCP"


async def test_admin_session_detail_404_when_missing(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _admin_token(client)
        r = await client.get(f"/api/v1/admin/sessions/{uuid.uuid4()}", headers=_h(token))
        assert r.status_code == 404


async def test_admin_session_detail_requires_admin(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_token = await _admin_token(client)
        user_token = await _make_user(client, admin_token, "u1")
        r = await client.get(f"/api/v1/admin/sessions/{uuid.uuid4()}", headers=_h(user_token))
        assert r.status_code == 403
