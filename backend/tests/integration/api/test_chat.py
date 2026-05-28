"""M4.7 集成测：`/api/v1/sessions/{sid}/messages` SSE + `/runs/{rid}` 取消。

通过 `app.state.agent_graph` 注入 Fake LangGraph，喂入 canned astream_events，
验证 10 类 SSE event 全部产出 + DB 持久化 + 取消路径。

文档锚 04-backend-api.md §M4.7。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk
from sqlalchemy import select

from app.db.models import Message, MessageCitation

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


async def _create_session(client: Any, token: str, title: str = "t") -> str:
    r = await client.post(
        "/api/v1/sessions",
        json={"title": title, "mode_default": "qa"},
        headers=_auth_headers(token),
    )
    assert r.status_code == 201
    return str(r.json()["id"])


# === Fake LangGraph ===


class _CannedGraph:
    """喂出齐全的 10 类事件 + 完整 final_state 的 fake graph。"""

    def __init__(
        self,
        *,
        events: list[dict[str, Any]] | None = None,
        final_state: dict[str, Any] | None = None,
    ) -> None:
        self._events = events
        self._final_state = final_state
        self.aupdate_state_calls: list[dict[str, Any]] = []

    async def astream_events(
        self, state: Any, *, config: Any, version: str
    ) -> AsyncIterator[dict[str, Any]]:
        for ev in self._events or []:
            yield ev
            # 让出控制权，确保 SSE 能拆帧
            await asyncio.sleep(0)
        # 顶层 graph end
        if self._final_state is not None:
            yield {
                "event": "on_chain_end",
                "name": "LangGraph",
                "data": {"output": self._final_state},
            }

    async def aupdate_state(self, *, config: Any, values: dict[str, Any]) -> None:
        self.aupdate_state_calls.append({"config": config, "values": values})


def _canned_full_run_events() -> list[dict[str, Any]]:
    """串成： classify start/end → retrieve start + chunks_hit + end →
    rerank start + chunks_rerank + end → generate start + token + end。"""
    return [
        {"event": "on_chain_start", "name": "classify", "data": {}},
        {
            "event": "on_chain_end",
            "name": "classify",
            "data": {"output": {"query_class": "definition", "complexity": "simple"}},
        },
        {"event": "on_chain_start", "name": "retrieve", "data": {}},
        {
            "event": "on_custom_event",
            "name": "chunks_hit",
            "data": {"chunks": [{"chunk_id": "c1", "score_dense": 0.9}]},
        },
        {
            "event": "on_chain_end",
            "name": "retrieve",
            "data": {"output": {"candidates": [1, 2, 3]}},
        },
        {"event": "on_chain_start", "name": "rerank", "data": {}},
        {
            "event": "on_custom_event",
            "name": "chunks_rerank",
            "data": {"chunks": [{"chunk_id": "c1", "spec_id": "23.501", "rerank_score": 0.88}]},
        },
        {
            "event": "on_chain_end",
            "name": "rerank",
            "data": {"output": {"reranked": [1]}},
        },
        {"event": "on_chain_start", "name": "generate", "data": {}},
        {
            "event": "on_chat_model_stream",
            "name": "generate",
            "data": {"chunk": AIMessageChunk(content="Hello ")},
        },
        {
            "event": "on_chat_model_stream",
            "name": "generate",
            "data": {"chunk": AIMessageChunk(content="world.")},
        },
        {"event": "on_chain_end", "name": "generate", "data": {"output": {}}},
    ]


def _canned_final_state() -> dict[str, Any]:
    return {
        "final_answer": "Hello world.",
        "citations": [
            {
                "chunk_id": "c1",
                "spec_id": "23.501",
                "section_path": "1.2.3",
                "rerank_score": 0.88,
            }
        ],
        "confidence": 0.77,
        "self_rag_verdict": "accept",
        "trace_id": "trace-xyz",
        "cancelled": False,
    }


def _parse_sse(payload: str) -> list[tuple[str, str]]:
    """切回 (event, data) tuple 列表；忽略 ping 注释行。"""
    out: list[tuple[str, str]] = []
    event: str | None = None
    data_lines: list[str] = []
    for line in payload.splitlines():
        if not line.strip():
            if event is not None:
                out.append((event, "\n".join(data_lines)))
            event = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue  # ping 注释
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if event is not None:
        out.append((event, "\n".join(data_lines)))
    return out


# === tests ===


async def test_full_sse_stream_emits_all_event_types_and_persists(
    app_and_state: Any, db_session: Any
) -> None:
    app, _, _ = app_and_state
    app.state.agent_graph = _CannedGraph(
        events=_canned_full_run_events(),
        final_state=_canned_final_state(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)
        r = await client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "what is AMF?"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)

    kinds = [k for k, _ in events]
    # 至少 9 类（cancelled 在另一条 test 验证）。
    expected_subset = {
        "run_start",
        "node_start",
        "node_end",
        "chunks_hit",
        "chunks_rerank",
        "token",
        "final",
        "end",
    }
    assert expected_subset.issubset(set(kinds)), kinds
    assert kinds[0] == "run_start"
    assert kinds[-1] == "end"
    # final 必在 end 之前
    assert kinds.index("final") < kinds.index("end")
    # token 必出现两次（"Hello "、"world."）
    assert kinds.count("token") == 2

    # DB 验证：assistant 行 content/citations/status
    res = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assistant = res.scalar_one()
    assert assistant.content == "Hello world."
    assert assistant.status == "ok"
    assert abs((assistant.confidence or 0.0) - 0.77) < 1e-6

    res = await db_session.execute(
        select(MessageCitation).where(MessageCitation.message_id == assistant.id)
    )
    cits = res.scalars().all()
    assert len(cits) == 1
    assert cits[0].chunk_id == "c1"
    assert cits[0].spec_id == "23.501"


class _FakeTitleClient:
    """首轮自动标题用的 fake LLM client：返回固定标题。"""

    def __init__(self, title: str) -> None:
        self._title = title
        self.calls: list[Any] = []

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(messages)
        return {"choices": [{"message": {"content": self._title}}]}


async def test_autotitle_on_first_turn_emits_title_event_and_persists(
    app_and_state: Any, db_session: Any
) -> None:
    """空标题会话首轮成功 → emit `title`（与 agent 并发，end 前）+ 回写 session.title。"""
    import uuid

    from app.db.models import Session as DBSession

    app, _, _ = app_and_state
    app.state.agent_graph = _CannedGraph(
        events=_canned_full_run_events(),
        final_state=_canned_final_state(),
    )
    title_cli = _FakeTitleClient("AMF 概述")
    app.state.title_client = title_cli

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token, title="")  # 空标题 → 触发自动标题
        r = await client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "what is AMF?"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)

    kinds = [k for k, _ in events]
    assert "title" in kinds, kinds
    # title 必在 end 之前；与 agent 并发，可以出现在 run_start 之后任意位置（含 final 之前）
    assert kinds.index("title") < kinds.index("end")
    assert kinds.index("run_start") < kinds.index("title")
    # title 事件 payload
    title_data = next(json.loads(d) for k, d in events if k == "title")
    assert title_data["session_id"] == sid
    assert title_data["title"] == "AMF 概述"
    # 用首个问题喂的 LLM
    assert title_cli.calls and "what is AMF?" in str(title_cli.calls[0])

    # DB 回写
    res = await db_session.execute(select(DBSession).where(DBSession.id == uuid.UUID(sid)))
    assert res.scalar_one().title == "AMF 概述"


async def test_autotitle_emitted_before_final_when_llm_is_fast(
    app_and_state: Any, db_session: Any
) -> None:
    """LIGHT 模型起标题快、agent 慢：title 应在 final 之前到达（与 agent 并发的核心收益）。

    防止有人改回"final 之后再串行起标题"导致前端 sidebar 标题落后于回答完成。
    """
    app, _, _ = app_and_state

    # agent 每个 event 之间 sleep 50ms，整条流约 600 ms 才出 final
    class _SlowGraph(_CannedGraph):
        async def astream_events(  # type: ignore[override]
            self, state: Any, *, config: Any, version: str
        ) -> AsyncIterator[dict[str, Any]]:
            for ev in self._events or []:
                yield ev
                await asyncio.sleep(0.05)
            if self._final_state is not None:
                yield {
                    "event": "on_chain_end",
                    "name": "LangGraph",
                    "data": {"output": self._final_state},
                }

    app.state.agent_graph = _SlowGraph(
        events=_canned_full_run_events(),
        final_state=_canned_final_state(),
    )

    class _FastTitleClient:
        def __init__(self, title: str) -> None:
            self._title = title

        async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
            # 几乎立刻返回（< agent 第一个节点的 50 ms 间隔）
            return {"choices": [{"message": {"content": self._title}}]}

    app.state.title_client = _FastTitleClient("AMF 概述")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token, title="")
        r = await client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "what is AMF?"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        kinds = [k for k, _ in _parse_sse(r.text)]

    assert "title" in kinds, kinds
    assert "final" in kinds, kinds
    # 核心断言：title 早于 final 到达（autotitle 与 agent 并发的收益）
    assert kinds.index("title") < kinds.index("final"), kinds


async def test_autotitle_skipped_when_title_already_set(
    app_and_state: Any, db_session: Any
) -> None:
    """非空标题会话不触发自动标题（不 emit title，不调用 LLM）。"""
    app, _, _ = app_and_state
    app.state.agent_graph = _CannedGraph(
        events=_canned_full_run_events(),
        final_state=_canned_final_state(),
    )
    title_cli = _FakeTitleClient("不应被用到")
    app.state.title_client = title_cli

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token, title="我的会话")
        r = await client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "what is AMF?"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        kinds = [k for k, _ in _parse_sse(r.text)]

    assert "title" not in kinds
    assert title_cli.calls == []


async def test_sse_cancelled_path_writes_cancelled_status(
    app_and_state: Any, db_session: Any
) -> None:
    """graph final_state.cancelled=True → 路由产 `cancelled` event + status='cancelled'。"""
    app, _, _ = app_and_state
    app.state.agent_graph = _CannedGraph(
        events=[
            {"event": "on_chain_start", "name": "classify", "data": {}},
            {
                "event": "on_chain_end",
                "name": "classify",
                "data": {"output": {"query_class": "unknown"}},
            },
        ],
        final_state={"cancelled": True, "final_answer": "", "citations": []},
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)
        r = await client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "stop me"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        kinds = [k for k, _ in _parse_sse(r.text)]
        assert "cancelled" in kinds
        assert kinds[-1] == "end"

    res = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assistant = res.scalar_one()
    assert assistant.status == "cancelled"
    assert assistant.content == ""


async def test_sse_error_path_writes_failed_status(app_and_state: Any, db_session: Any) -> None:
    """graph 抛异常 → 产 `error` + status='failed'。"""

    class _BoomGraph:
        async def astream_events(self, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
            yield {"event": "on_chain_start", "name": "classify", "data": {}}
            raise RuntimeError("agent_boom")

        async def aupdate_state(self, **kwargs: Any) -> None:
            return None

    app, _, _ = app_and_state
    app.state.agent_graph = _BoomGraph()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)
        r = await client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "boom"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 200
        kinds = [k for k, _ in _parse_sse(r.text)]
        assert "error" in kinds
        assert kinds[-1] == "end"

    res = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assistant = res.scalar_one()
    assert assistant.status == "failed"


async def test_delete_cancels_inflight_sse_stream_via_race(
    app_and_state: Any, db_session: Any
) -> None:
    """F-1：并发 SSE + DELETE → race loop 监测到 cancel_event → 立即终止 + emit cancelled。

    用 hang 图模拟 LLM streaming 卡在网络等待；如果没有 race 机制，DELETE 只设
    aupdate_state flag 无效，SSE 会卡死（M4.8 best-effort 缺陷）。
    """
    block_event = asyncio.Event()

    class _HangGraph:
        def __init__(self) -> None:
            self.aupdate_state_calls: list[dict[str, Any]] = []

        async def astream_events(
            self, state: Any, *, config: Any, version: str
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"event": "on_chain_start", "name": "classify", "data": {}}
            yield {"event": "on_chain_end", "name": "classify", "data": {"output": {}}}
            # 模拟 LLM 卡在 streaming —— 没有 cancel 机制就永远不返回
            await block_event.wait()
            # 解开 block 后才走到这（不应该被走到）
            yield {"event": "on_chain_end", "name": "LangGraph", "data": {"output": {}}}

        async def aupdate_state(self, *, config: Any, values: dict[str, Any]) -> None:
            self.aupdate_state_calls.append({"config": config, "values": values})

    app, _, _ = app_and_state
    graph = _HangGraph()
    app.state.agent_graph = graph

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)

        async def _cancel_when_registered() -> tuple[int | None, str | None]:
            # 等 send_message 把 cancel_event 注册到 registry → DELETE
            for _ in range(100):
                reg = getattr(app.state, "in_flight_cancels", {})
                if reg:
                    rid = next(iter(reg.keys()))
                    r = await client.delete(
                        f"/api/v1/sessions/{sid}/runs/{rid}",
                        headers=_auth_headers(token),
                    )
                    return r.status_code, rid
                await asyncio.sleep(0.02)
            return None, None

        sse_task = asyncio.create_task(
            client.post(
                f"/api/v1/sessions/{sid}/messages",
                json={"content": "hang"},
                headers=_auth_headers(token),
            )
        )
        cancel_status, run_id = await _cancel_when_registered()
        assert cancel_status == 204, "DELETE 没在 5s 内找到 in-flight run"
        assert run_id is not None

        resp = await sse_task
        assert resp.status_code == 200
        kinds = [k for k, _ in _parse_sse(resp.text)]
        assert "cancelled" in kinds, f"SSE 缺 cancelled 事件，实际：{kinds}"
        assert kinds[-1] == "end"

    res = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assistant = res.scalar_one()
    assert assistant.status == "cancelled"
    # registry 已清
    assert app.state.in_flight_cancels == {}
    # aupdate_state 也被调过（双通道）
    assert any(c["values"].get("cancelled") for c in graph.aupdate_state_calls)


async def test_cancel_run_delegates_to_aupdate_state(app_and_state: Any) -> None:
    app, _, _ = app_and_state
    graph = _CannedGraph(events=[], final_state=_canned_final_state())
    app.state.agent_graph = graph

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)
        r = await client.delete(
            f"/api/v1/sessions/{sid}/runs/run-xyz", headers=_auth_headers(token)
        )
        assert r.status_code == 204

    assert len(graph.aupdate_state_calls) == 1
    vals = graph.aupdate_state_calls[0]["values"]
    assert vals == {"cancelled": True, "run_id": "run-xyz"}


async def test_send_message_rejects_archived_branch(app_and_state: Any, db_session: Any) -> None:
    import uuid

    from sqlalchemy import update

    from app.db.models import Session as DBSession

    app, _, _ = app_and_state
    app.state.agent_graph = _CannedGraph(events=[], final_state=_canned_final_state())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        sid = await _create_session(client, token)
        await db_session.execute(
            update(DBSession).where(DBSession.id == uuid.UUID(sid)).values(status="archived_branch")
        )
        await db_session.commit()
        r = await client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "x"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 409
        assert r.json()["code"] == "session_archived"


async def test_send_message_session_not_found(app_and_state: Any) -> None:
    import uuid

    app, _, _ = app_and_state
    app.state.agent_graph = _CannedGraph(events=[], final_state=_canned_final_state())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client)
        bogus = str(uuid.uuid4())
        r = await client.post(
            f"/api/v1/sessions/{bogus}/messages",
            json={"content": "x"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 404
