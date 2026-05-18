"""M4.5 集成测：checkpoint 操作端到端。

口径 = `docs/03-development/03-agent.md §11 / §12 / §14 M4.5`。

4 个 `[auto]` 场景：
1. 中途取消：cancel_run 后续跑在节点边界 interrupt 停下
2. 暂停 → 关进程 → 重启 → 恢复续跑：构造新 graph 实例复用同 saver，验证状态持久 + 续跑
3. fork：从历史 checkpoint 起新会话，老会话状态保留不变（"老会话变只读"由后端在
   PG sessions 表标 archived_branch，本测只验 LangGraph 层 state 隔离）
4. rollback 一致性：rollback(N) 后 head state = 第 N 个老 checkpoint 的 values + history 变短

实现注意：不需要真 LLM；用一个 3 节点 mini-graph 模拟 agent 的"边界 + 状态字段"语义。
节点开头同步调用 `langgraph.types.interrupt` 检查 cancelled / paused，与生产节点一致（§11）。

LangGraph v1+：`interrupt(value)` 会把 `__interrupt__` 注入返回字典 + snap.next 指向被打断
的节点 + snap.tasks 里能拿到 Interrupt(value=...) 元数据。所以本测断言 "中途取消/暂停"
看这三个信号，而不是 `pytest.raises`。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from app.agent import checkpoint as ckpt

pytestmark = pytest.mark.integration


class _S(BaseModel):
    """Mini-agent state；字段与生产 AgentState 子集对齐。"""

    model_config = ConfigDict(extra="forbid")

    user_input: str = ""
    paused: bool = False
    cancelled: bool = False
    run_id: str | None = None
    retry_count: int = 0
    stage: str = "init"
    log: list[str] = Field(default_factory=list)


def _build_mini(saver: InMemorySaver) -> CompiledStateGraph:
    """3 节点：classify → retrieve → generate；每个节点都开头检查 paused/cancelled。"""

    def _guard(s: _S) -> None:
        if s.cancelled:
            interrupt({"reason": "cancelled by user"})
        if s.paused:
            interrupt({"reason": "paused by user"})

    def classify(s: _S) -> dict:
        _guard(s)
        return {"stage": "classified", "log": [*s.log, "classify"]}

    def retrieve(s: _S) -> dict:
        _guard(s)
        return {"stage": "retrieved", "log": [*s.log, "retrieve"]}

    def generate(s: _S) -> dict:
        _guard(s)
        return {"stage": "generated", "log": [*s.log, "generate"]}

    b: StateGraph = StateGraph(_S)
    b.add_node("classify", classify)
    b.add_node("retrieve", retrieve)
    b.add_node("generate", generate)
    b.add_edge(START, "classify")
    b.add_edge("classify", "retrieve")
    b.add_edge("retrieve", "generate")
    b.add_edge("generate", END)
    return b.compile(checkpointer=saver)


# ---- 1. cancel ----------------------------------------------------------


async def test_cancel_run_stops_at_next_node_boundary() -> None:
    saver = InMemorySaver()
    graph = _build_mini(saver)
    cfg = {"configurable": {"thread_id": "sess-cancel"}}

    # 第一次跑跑完，会话已经"在线"
    await graph.ainvoke({"user_input": "q1"}, config=cfg)
    snap = await graph.aget_state(cfg)
    assert snap.values["stage"] == "generated"
    assert snap.values["log"] == ["classify", "retrieve", "generate"]

    # 标 cancelled，触发新一轮 invoke 时 classify 立刻 interrupt
    await ckpt.cancel_run(graph, "sess-cancel")
    out = await graph.ainvoke({"user_input": "q2"}, config=cfg)

    # LangGraph v1+：__interrupt__ 进返回字典，不抛
    assert "__interrupt__" in out
    interrupts = out["__interrupt__"]
    assert any("cancelled" in str(i.value) for i in interrupts)

    # snap.next 仍指向被中断的 classify；q2 未 log
    snap2 = await graph.aget_state(cfg)
    assert snap2.values["cancelled"] is True
    assert snap2.next == ("classify",)
    assert "classify" not in snap2.values["log"][3:]  # q1 三步之后没新增


# ---- 2. pause → "进程重启" → resume → 续跑 ------------------------------


async def test_pause_resume_across_process_restart() -> None:
    """同一 saver、两个 graph 实例：模拟 "暂停 → 关进程 → 重启 → 续跑"。

    步骤：
    1. graph_a + saver：跑到完成
    2. pause_run + 模拟新 user_input 进 retry：state.paused=True → interrupt
    3. 丢掉 graph_a；graph_b 重新 compile 同一个 saver（"重启进程"）
    4. resume_run（清 paused）→ ainvoke(None, cfg) 续跑 → 走完
    """
    saver = InMemorySaver()
    graph_a = _build_mini(saver)
    cfg = {"configurable": {"thread_id": "sess-pause"}}

    await graph_a.ainvoke({"user_input": "q1"}, config=cfg)

    # 暂停：在保留 state 的前提下，让下一节点边界停下
    await ckpt.pause_run(graph_a, "sess-pause", run_id="run-1")

    # 触发新一轮 invoke → classify 立刻被 paused 拦截
    out = await graph_a.ainvoke({"user_input": "q2"}, config=cfg)
    assert "__interrupt__" in out
    assert any("paused" in str(i.value) for i in out["__interrupt__"])

    # === "关进程"：丢掉 graph_a ===
    del graph_a
    graph_b = _build_mini(saver)

    # 状态被 saver 保留：paused=True, q2 已被 input 写入
    snap_mid = await graph_b.aget_state(cfg)
    assert snap_mid.values["paused"] is True
    assert snap_mid.values["user_input"] == "q2"

    # resume_run 清 paused，再用 None 续跑（从最后 checkpoint 继续）
    await ckpt.resume_run(graph_b, "sess-pause")
    await graph_b.ainvoke(None, config=cfg)

    snap_end = await graph_b.aget_state(cfg)
    assert snap_end.values["paused"] is False
    assert snap_end.values["stage"] == "generated"
    # log 包含两轮：q1 跑完了 3 节点，q2 也跑完 3 节点（resume 后从 classify 重跑）
    assert snap_end.values["log"].count("generate") == 2


# ---- 3. fork ------------------------------------------------------------


async def test_fork_from_creates_independent_branch() -> None:
    saver = InMemorySaver()
    graph = _build_mini(saver)
    cfg = {"configurable": {"thread_id": "sess-main"}}

    await graph.ainvoke({"user_input": "main query"}, config=cfg)
    history = await ckpt.list_checkpoints(graph, "sess-main")

    # 找 retrieve 完成后的 checkpoint（next_nodes == ("generate",)）
    mid = next(s for s in history if s.next_nodes == ("generate",))

    new_cfg = await ckpt.fork_from(
        graph,
        "sess-main",
        mid.checkpoint_id,
        "sess-fork",
        new_user_message="fork query",
    )
    # 续跑新分支
    await graph.ainvoke(None, config=new_cfg)

    fork_snap = await graph.aget_state(new_cfg)
    main_snap = await graph.aget_state(cfg)

    # 新分支：从 retrieve 完成后开始，user_input 被覆写
    assert fork_snap.values["user_input"] == "fork query"
    assert fork_snap.values["stage"] == "generated"
    # 老分支：依旧停在 "main query" 跑完的终态，不被影响
    assert main_snap.values["user_input"] == "main query"
    assert main_snap.values["stage"] == "generated"

    # 两个 thread_id 各自有独立 history
    main_hist = await ckpt.list_checkpoints(graph, "sess-main")
    fork_hist = await ckpt.list_checkpoints(graph, "sess-fork")
    assert len(main_hist) >= 3
    assert len(fork_hist) >= 1
    # 两个 history 没有共享 checkpoint_id
    main_ids = {s.checkpoint_id for s in main_hist}
    fork_ids = {s.checkpoint_id for s in fork_hist}
    assert main_ids.isdisjoint(fork_ids)


# ---- 4. rollback 一致性 ------------------------------------------------


async def test_rollback_state_and_history_consistent() -> None:
    saver = InMemorySaver()
    graph = _build_mini(saver)
    cfg = {"configurable": {"thread_id": "sess-roll"}}

    await graph.ainvoke({"user_input": "q1"}, config=cfg)
    full = await ckpt.list_checkpoints(graph, "sess-roll")
    assert len(full) >= 4  # input + classify + retrieve + generate

    # 回滚 1 步：head 应该回到 retrieve 完成的状态（stage="retrieved"）
    head = await ckpt.rollback(graph, "sess-roll", last_n=1)
    assert head is not None

    snap = await graph.aget_state(cfg)
    # rollback 用 history[1]（也就是 generate 跑完之前的那个 checkpoint）的 values
    # → stage 应该是 "retrieved"，log 应该没有 "generate"
    assert snap.values["stage"] == "retrieved"
    assert "generate" not in snap.values["log"]

    # history 长度严格变短
    after = await ckpt.list_checkpoints(graph, "sess-roll")
    assert len(after) < len(full)

    # 续跑（从回滚点）→ generate 重新跑一次
    await graph.ainvoke(None, config=cfg)
    final = await graph.aget_state(cfg)
    assert final.values["stage"] == "generated"
    assert final.values["log"].count("generate") == 1


async def test_rollback_wipes_thread_when_n_exceeds_history() -> None:
    saver = InMemorySaver()
    graph = _build_mini(saver)
    cfg = {"configurable": {"thread_id": "sess-wipe"}}
    await graph.ainvoke({"user_input": "q1"}, config=cfg)

    out = await ckpt.rollback(graph, "sess-wipe", last_n=999)
    assert out is None
    assert (await ckpt.list_checkpoints(graph, "sess-wipe")) == []
