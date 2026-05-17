"""checkpoint.py 5 个纯函数 + Langfuse 工厂单测。

口径见 `docs/03-development/03-agent.md §12`。
- 用 InMemorySaver + 极简 2 节点 graph 自包含测，绕开 PG 依赖
- 5 个操作：list / pause / cancel / resume / fork / rollback
- Langfuse 工厂：缺 key 返回 None；metadata 构造 langfuse_session_id 等字段
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field

from app.agent import checkpoint as ckpt
from app.agent import langfuse_handler


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_input: str = ""
    paused: bool = False
    cancelled: bool = False
    run_id: str | None = None
    retry_count: int = 0
    counter: int = 0
    history: list[str] = Field(default_factory=list)


def _build_test_graph(saver: InMemorySaver) -> CompiledStateGraph:
    """2 节点：n1 加 counter + 写 history；n2 翻倍 counter。"""

    def n1(s: _S) -> dict:
        return {"counter": s.counter + 1, "history": [*s.history, "n1"]}

    def n2(s: _S) -> dict:
        return {"counter": s.counter * 2, "history": [*s.history, "n2"]}

    b: StateGraph = StateGraph(_S)
    b.add_node("n1", n1)
    b.add_node("n2", n2)
    b.add_edge(START, "n1")
    b.add_edge("n1", "n2")
    b.add_edge("n2", END)
    return b.compile(checkpointer=saver)


# ---- list_checkpoints ---------------------------------------------------


async def test_list_checkpoints_returns_history_newest_first() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 0}, config=cfg)

    summaries = await ckpt.list_checkpoints(graph, "s1")
    assert len(summaries) >= 3  # init + n1 + n2 at least
    assert summaries[0].created_at  # newest 有时间戳
    # parent 链：除根外都有 parent
    assert summaries[-1].parent_checkpoint_id is None
    assert all(s.parent_checkpoint_id is not None for s in summaries[:-1])


async def test_list_checkpoints_limit() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 0}, config=cfg)

    summaries = await ckpt.list_checkpoints(graph, "s1", limit=2)
    assert len(summaries) == 2


# ---- pause / cancel / resume -------------------------------------------


async def test_pause_run_sets_paused_field() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 0}, config=cfg)

    await ckpt.pause_run(graph, "s1", run_id="run-abc")
    snap = await graph.aget_state(cfg)
    assert snap.values["paused"] is True
    assert snap.values["run_id"] == "run-abc"


async def test_cancel_run_sets_cancelled_field() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 0}, config=cfg)

    await ckpt.cancel_run(graph, "s1")
    snap = await graph.aget_state(cfg)
    assert snap.values["cancelled"] is True


async def test_resume_run_clears_paused() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 0}, config=cfg)
    await ckpt.pause_run(graph, "s1", run_id="r1")
    assert (await graph.aget_state(cfg)).values["paused"] is True

    out = await ckpt.resume_run(graph, "s1")
    assert out["configurable"]["thread_id"] == "s1"
    assert (await graph.aget_state(cfg)).values["paused"] is False


# ---- fork_from ----------------------------------------------------------


async def test_fork_from_copies_state_to_new_thread() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 5}, config=cfg)

    history = await ckpt.list_checkpoints(graph, "s1")
    # 找一个 mid-history checkpoint（n1 完成后、n2 之前 → next_nodes == ("n2",)）
    mid = next(s for s in history if s.next_nodes == ("n2",))
    new_cfg = await ckpt.fork_from(
        graph,
        "s1",
        mid.checkpoint_id,
        "s2",
        new_user_message="forked query",
    )
    assert new_cfg["configurable"]["thread_id"] == "s2"
    snap2 = await graph.aget_state(new_cfg)
    assert snap2.values["counter"] == 6  # n1 后是 5+1
    assert snap2.values["user_input"] == "forked query"
    # 原 thread 保持不动
    snap1 = await graph.aget_state(cfg)
    assert snap1.values["counter"] == 12  # 5+1 然后 *2


async def test_fork_from_without_checkpointer_raises() -> None:
    b: StateGraph = StateGraph(_S)

    async def n(s: _S) -> dict:
        return {"counter": s.counter + 1}

    b.add_node("n", n)
    b.add_edge(START, "n")
    b.add_edge("n", END)
    graph = b.compile()  # 无 checkpointer

    with pytest.raises(RuntimeError, match="checkpointer"):
        await ckpt.fork_from(graph, "s1", "ckpt-x", "s2")


async def test_fork_from_unknown_checkpoint_raises() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 0}, config=cfg)

    with pytest.raises(ValueError, match="checkpoint not found"):
        await ckpt.fork_from(graph, "s1", "nonexistent-ckpt-id", "s2")


# ---- rollback -----------------------------------------------------------


async def test_rollback_truncates_to_target_state() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 3}, config=cfg)

    pre = await ckpt.list_checkpoints(graph, "s1")
    # rollback last 1（即把 n2 这步撤掉，回到 n1 后的状态 counter=4）
    head = await ckpt.rollback(graph, "s1", last_n=1)
    assert head is not None
    snap = await graph.aget_state(cfg)
    assert snap.values["counter"] == 4
    # history 已经被 delete_thread + 重新 put → 数量比之前少
    post = await ckpt.list_checkpoints(graph, "s1")
    assert len(post) < len(pre)


async def test_rollback_zero_is_noop() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 1}, config=cfg)

    head_before = await ckpt.list_checkpoints(graph, "s1", limit=1)
    out = await ckpt.rollback(graph, "s1", last_n=0)
    head_after = await ckpt.list_checkpoints(graph, "s1", limit=1)
    assert out is not None
    assert head_after[0].checkpoint_id == head_before[0].checkpoint_id


async def test_rollback_excessive_n_wipes_thread() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    cfg = {"configurable": {"thread_id": "s1"}}
    await graph.ainvoke({"counter": 1}, config=cfg)

    out = await ckpt.rollback(graph, "s1", last_n=999)
    assert out is None
    summaries = await ckpt.list_checkpoints(graph, "s1")
    assert summaries == []


async def test_rollback_negative_raises() -> None:
    saver = InMemorySaver()
    graph = _build_test_graph(saver)
    with pytest.raises(ValueError, match="last_n"):
        await ckpt.rollback(graph, "s1", last_n=-1)


# ---- Langfuse 工厂 -------------------------------------------------------


def _settings_without_langfuse():
    from pydantic import SecretStr

    from app.core.config import Settings

    s = Settings(LITELLM_API_KEY="k")
    return s.model_copy(
        update={"LANGFUSE_PUBLIC_KEY": SecretStr(""), "LANGFUSE_SECRET_KEY": SecretStr("")}
    )


def test_init_langfuse_returns_none_without_keys() -> None:
    langfuse_handler._reset_for_tests()
    s = _settings_without_langfuse()
    assert langfuse_handler.init_langfuse(s) is None
    # 二次调用走 cache 仍 None
    assert langfuse_handler.init_langfuse(s) is None


def test_build_callback_handler_returns_none_without_keys() -> None:
    langfuse_handler._reset_for_tests()
    s = _settings_without_langfuse()
    assert langfuse_handler.build_callback_handler(s) is None


def test_build_trace_metadata_emits_langfuse_keys() -> None:
    m = langfuse_handler.build_trace_metadata(
        session_id="sess-1",
        user_id="u-1",
        mode="qa",
        extra={"foo": "bar"},
    )
    assert m["langfuse_session_id"] == "sess-1"
    assert m["langfuse_user_id"] == "u-1"
    assert m["mode"] == "qa"
    assert m["foo"] == "bar"
    assert m["app"] == "tgpp"


def test_build_trace_metadata_omits_none_fields() -> None:
    m = langfuse_handler.build_trace_metadata()
    assert "langfuse_session_id" not in m
    assert "langfuse_user_id" not in m
    assert "mode" not in m
    assert m == {"app": "tgpp"}
