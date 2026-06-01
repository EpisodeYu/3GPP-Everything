"""M4.8 集成测：`/api/v1/sessions/{sid}/...` checkpoint 5 路由。

通过 `app.state.agent_graph` 注入 Fake LangGraph，验证 pause / resume / list /
fork / rollback 路由的契约 + DB 副作用 + 跑中冲突 409。

文档锚 04-backend-api.md §M4.8。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agent.checkpoint import CheckpointSummary
from app.db.models import Message
from app.db.models import Session as DBSession

from .test_auth import _bootstrap_admin, _login


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _new_user_token(client: Any, username: str = "u1") -> str:
    await _bootstrap_admin(client)
    admin = await _login(client, "admin1", "passw0rd!")
    res = await client.post(
        "/api/v1/users",
        json={"username": username, "password": "passw0rd!", "role": "user"},
        headers=_auth_headers(admin["access_token"]),
    )
    assert res.status_code == 201, res.text
    out = await _login(client, username, "passw0rd!")
    return str(out["access_token"])


async def _create_session(client: Any, token: str) -> str:
    r = await client.post(
        "/api/v1/sessions",
        json={"title": "t", "mode_default": "qa"},
        headers=_auth_headers(token),
    )
    assert r.status_code == 201
    return str(r.json()["id"])


# === Fake LangGraph ===


class _CheckpointGraph:
    """覆盖 checkpoint 操作：暴露 aupdate_state / aget_state_history hooks。"""

    def __init__(self, *, summaries: list[CheckpointSummary] | None = None) -> None:
        self.summaries = summaries or []
        self.aupdate_state_calls: list[dict[str, Any]] = []
        self.fork_calls: list[dict[str, Any]] = []
        self.rollback_calls: list[dict[str, Any]] = []

    async def aupdate_state(
        self,
        config: Any = None,
        values: dict[str, Any] | None = None,
    ) -> None:
        self.aupdate_state_calls.append({"config": config, "values": values})

    async def astream_events(
        self, state: Any, *, config: Any, version: str
    ) -> AsyncIterator[dict[str, Any]]:
        # 续跑：直接给 final_state 让 SSE 收尾
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "data": {
                "output": {
                    "final_answer": "resumed",
                    "citations": [],
                    "confidence": 0.5,
                    "self_rag_verdict": "accept",
                    "trace_id": "trace-resume",
                    "cancelled": False,
                }
            },
        }


# === tests ===


async def test_pause_marks_session_paused_and_calls_aupdate(
    app_and_state: Any, db_session: Any
) -> None:
    app, _, _ = app_and_state
    graph = _CheckpointGraph()
    app.state.agent_graph = graph

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)

        r = await client.post(
            f"/api/v1/sessions/{sid}/runs/run-abc/pause",
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "paused"
        assert body["run_id"] == "run-abc"

    # DB 验证
    res = await db_session.execute(select(DBSession).where(DBSession.id == uuid.UUID(sid)))
    s = res.scalar_one()
    assert s.status == "paused"

    assert len(graph.aupdate_state_calls) == 1
    assert graph.aupdate_state_calls[0]["values"] == {"paused": True, "run_id": "run-abc"}


async def test_list_checkpoints_maps_summary_payload(
    app_and_state: Any,
) -> None:
    app, _, _ = app_and_state
    fake_summaries = [
        CheckpointSummary(
            checkpoint_id="ck-1",
            parent_checkpoint_id=None,
            created_at="2026-05-18T12:00:00Z",
            next_nodes=("classify",),
            last_node=None,
        ),
        CheckpointSummary(
            checkpoint_id="ck-2",
            parent_checkpoint_id="ck-1",
            created_at="2026-05-18T12:00:05Z",
            next_nodes=("generate",),
            last_node="retrieve",
        ),
    ]

    class _G:
        async def aget_state_history(self, cfg: Any) -> AsyncIterator[Any]:
            class _Snap:
                def __init__(self, s: CheckpointSummary, parent_id: str | None) -> None:
                    self.config = {
                        "configurable": {"thread_id": "x", "checkpoint_id": s.checkpoint_id}
                    }
                    self.parent_config = (
                        {"configurable": {"checkpoint_id": parent_id}} if parent_id else None
                    )
                    self.created_at = s.created_at
                    self.next = s.next_nodes
                    self.metadata = {"writes": {s.last_node: {}}} if s.last_node else {"writes": {}}

            for s in fake_summaries:
                yield _Snap(s, s.parent_checkpoint_id)

    app.state.agent_graph = _G()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)

        r = await client.get(f"/api/v1/sessions/{sid}/checkpoints", headers=_auth_headers(token))
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 2
        assert items[0]["checkpoint_id"] == "ck-1"
        assert items[0]["next_nodes"] == ["classify"]
        assert items[1]["last_node"] == "retrieve"


async def test_fork_creates_new_session_keeps_original_active(
    app_and_state: Any, db_session: Any
) -> None:
    """fork 后原会话保持 active（2026-06-01 行为变更）。

    旧实现会把原会话标 `archived_branch` 强制转为只读；新行为里原会话不动，
    用户可以继续在原会话发问，新会话作为分叉独立存在。
    """
    app, _, _ = app_and_state

    class _G:
        def __init__(self) -> None:
            self.checkpointer = object()  # 非 None 即可
            self.fork_calls: list[dict[str, Any]] = []

        async def aget_state(self, cfg: Any) -> Any:
            class _Snap:
                values: ClassVar[dict[str, Any]] = {
                    "user_input": "main",
                    "stage": "retrieved",
                }

            return _Snap()

        async def aupdate_state(self, cfg: Any, values: dict[str, Any]) -> None:
            self.fork_calls.append({"cfg": cfg, "values": values})

    graph = _G()
    app.state.agent_graph = graph

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)

        r = await client.post(
            f"/api/v1/sessions/{sid}/fork",
            json={
                "checkpoint_id": "ck-mid",
                "new_user_message": "alt query",
                "title": "branch",
            },
            headers=_auth_headers(token),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        new_sid = body["new_session"]["id"]
        assert body["new_session"]["title"] == "branch"
        assert body["new_session"]["forked_from_session_id"] == sid
        assert body["new_session"]["forked_from_checkpoint_id"] == "ck-mid"

    # 原会话 仍 active（不再 archive）
    res = await db_session.execute(select(DBSession).where(DBSession.id == uuid.UUID(sid)))
    s = res.scalar_one()
    assert s.status == "active"

    # 新会话存在 + active
    res = await db_session.execute(select(DBSession).where(DBSession.id == uuid.UUID(new_sid)))
    n = res.scalar_one()
    assert n.status == "active"
    assert n.forked_from_session_id == uuid.UUID(sid)

    # graph.fork_from 调用：写新 thread state，user_input 被覆盖
    assert any(c["values"].get("user_input") == "alt query" for c in graph.fork_calls)


async def test_rollback_deletes_last_n_rounds_and_calls_graph(
    app_and_state: Any, db_session: Any
) -> None:
    """rollback last_n=1 = 删最近 1 轮（user-2 + assistant-2，留下 user-1+assistant-1）。

    "一轮" = 一个 user message + 它之后该会话的所有 message。本测两轮共 4 条
    （u1, a1, u2, a2，每轮 user 比 assistant 早 10 微秒，跨轮间隔 5 秒）。
    """
    app, _, _ = app_and_state

    class _G:
        def __init__(self) -> None:
            self.checkpointer = _Saver()
            self.rollback_n: int | None = None

        async def aget_state_history(self, cfg: Any) -> AsyncIterator[Any]:
            for i in range(5):

                class _Snap:
                    def __init__(self, idx: int) -> None:
                        self.config = {
                            "configurable": {"thread_id": "x", "checkpoint_id": f"ck-{idx}"}
                        }
                        self.parent_config = None
                        self.created_at = f"t-{idx}"
                        self.next = ()
                        self.metadata = {}
                        self.values = {"stage": f"step-{idx}"}

                yield _Snap(i)

        async def aupdate_state(self, cfg: Any, values: dict[str, Any]) -> None:
            return None

    class _Saver:
        async def adelete_thread(self, sid: str) -> None:
            return None

    graph = _G()
    app.state.agent_graph = graph

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)

        # 灌 2 轮（共 4 条）已完成 message：每轮 user 比 assistant 早 10us，
        # 第二轮整体比第一轮晚 5 秒
        import datetime as _dt

        sid_uuid = uuid.UUID(sid)
        base = _dt.datetime(2026, 5, 18, 12, 0, 0, tzinfo=_dt.UTC)
        rounds = [
            ("user", "u-1", base),
            ("assistant", "a-1", base + _dt.timedelta(microseconds=10)),
            ("user", "u-2", base + _dt.timedelta(seconds=5)),
            ("assistant", "a-2", base + _dt.timedelta(seconds=5, microseconds=10)),
        ]
        for role, content, ts in rounds:
            db_session.add(
                Message(
                    session_id=sid_uuid,
                    role=role,
                    content=content,
                    status="ok",
                    created_at=ts,
                )
            )
        await db_session.commit()

        r = await client.post(
            f"/api/v1/sessions/{sid}/rollback",
            json={"last_n": 1},
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted_messages"] == 2

    res = await db_session.execute(select(Message).where(Message.session_id == uuid.UUID(sid)))
    remaining = sorted(res.scalars().all(), key=lambda m: m.created_at)
    assert [m.content for m in remaining] == ["u-1", "a-1"]


async def test_rollback_drops_assistant_when_user_and_assistant_share_created_at(
    app_and_state: Any, db_session: Any
) -> None:
    """**关键回归**：user_msg 与 assistant_msg 的 created_at 完全相同时，
    rollback 仍能正确把这一轮（含 assistant）整体删掉。

    旧实现按 `order_by(created_at desc).limit(1)` + last_n=1 时常常只删
    user、留下 assistant。新实现按 user 锚点 + cutoff，应一并删除。
    """
    app, _, _ = app_and_state

    class _G:
        def __init__(self) -> None:
            self.checkpointer = _Saver()

        async def aget_state_history(self, cfg: Any) -> AsyncIterator[Any]:
            return
            yield  # pragma: no cover

        async def aupdate_state(self, cfg: Any, values: dict[str, Any]) -> None:
            return None

    class _Saver:
        async def adelete_thread(self, sid: str) -> None:
            return None

    app.state.agent_graph = _G()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)

        import datetime as _dt

        sid_uuid = uuid.UUID(sid)
        same_ts = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.UTC)
        # 故意完全同 created_at，模拟 PG `now()` 在同事务返回相同时间戳的情况
        db_session.add(
            Message(
                session_id=sid_uuid,
                role="user",
                content="q",
                status="ok",
                created_at=same_ts,
            )
        )
        db_session.add(
            Message(
                session_id=sid_uuid,
                role="assistant",
                content="a",
                status="ok",
                created_at=same_ts,
            )
        )
        await db_session.commit()

        r = await client.post(
            f"/api/v1/sessions/{sid}/rollback",
            json={"last_n": 1},
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["deleted_messages"] == 2

    res = await db_session.execute(select(Message).where(Message.session_id == uuid.UUID(sid)))
    assert res.scalars().all() == []


async def test_rollback_with_inflight_run_returns_409(app_and_state: Any, db_session: Any) -> None:
    app, _, _ = app_and_state
    app.state.agent_graph = _CheckpointGraph()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)

        # 注入 stub assistant message：status='ok' AND content='' → inflight
        db_session.add(
            Message(
                session_id=uuid.UUID(sid),
                role="assistant",
                content="",
                status="ok",
            )
        )
        await db_session.commit()

        r = await client.post(
            f"/api/v1/sessions/{sid}/rollback",
            json={"last_n": 1},
            headers=_auth_headers(token),
        )
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "rollback_conflicts_with_active_run"


async def test_resume_requires_paused_status(app_and_state: Any) -> None:
    """active session 不能 resume → 409。"""
    app, _, _ = app_and_state
    app.state.agent_graph = _CheckpointGraph()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)
        r = await client.post(
            f"/api/v1/sessions/{sid}/resume",
            headers=_auth_headers(token),
        )
        assert r.status_code == 409
        assert r.json()["code"] == "session_not_paused"


async def test_resume_clears_paused_and_streams_sse(app_and_state: Any, db_session: Any) -> None:
    """pause → resume：清 paused flag、返回 SSE stream、session 回 active。"""
    app, _, _ = app_and_state
    graph = _CheckpointGraph()
    app.state.agent_graph = graph

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)

        # pause
        r = await client.post(
            f"/api/v1/sessions/{sid}/runs/run-x/pause",
            headers=_auth_headers(token),
        )
        assert r.status_code == 200

        # resume → 返回 SSE
        r = await client.post(
            f"/api/v1/sessions/{sid}/resume",
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        # SSE body 至少含 run_start 与 end
        assert "event: run_start" in r.text
        assert "event: final" in r.text
        assert "event: end" in r.text

    # session 回 active
    res = await db_session.execute(select(DBSession).where(DBSession.id == uuid.UUID(sid)))
    s = res.scalar_one()
    assert s.status == "active"

    # graph 收到清 paused 的 aupdate_state（{"paused": False}）
    assert any(c["values"].get("paused") is False for c in graph.aupdate_state_calls)


async def test_checkpoint_routes_require_auth(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    transport = ASGITransport(app=app)
    bogus = str(uuid.uuid4())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path, method in [
            (f"/api/v1/sessions/{bogus}/runs/r/pause", "POST"),
            (f"/api/v1/sessions/{bogus}/resume", "POST"),
            (f"/api/v1/sessions/{bogus}/checkpoints", "GET"),
            (f"/api/v1/sessions/{bogus}/fork", "POST"),
            (f"/api/v1/sessions/{bogus}/rollback", "POST"),
        ]:
            r = await client.request(method, path, json={})
            assert r.status_code == 401, (path, r.text)


async def test_fork_with_unknown_session_returns_404(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    app.state.agent_graph = _CheckpointGraph()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        bogus = str(uuid.uuid4())
        r = await client.post(
            f"/api/v1/sessions/{bogus}/fork",
            json={"checkpoint_id": "ck", "title": "x"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 404
