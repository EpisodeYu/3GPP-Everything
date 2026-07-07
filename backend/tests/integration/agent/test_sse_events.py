"""M4.3 流式 SSE event 序列：assert LangGraph 端产出事件符合 §7 表。

口径：`docs/03-development/03-agent.md §7` + §14 M4.3 第 4 条。
LangGraph 端通过两套 stream 暴露给 backend：
  - `astream(stream_mode=["updates", "custom"])` 给 backend 拿"节点完成 / 自定义"事件
  - `astream_events(version="v2")` 给 backend 拿细粒度（含 token 流）事件

本测试关注两件事：
  1. updates+custom 流：节点完成顺序 + chunks_hit 自定义事件穿插在 retrieve 完成后
  2. astream_events：on_chain_start / on_chain_end 覆盖每个节点（smoke）
"""

from __future__ import annotations

import json

import pytest

from app.agent.graph import build_graph
from app.agent.state import AgentState

from ...unit.agent.conftest import (
    StubDense,
    StubLLM,
    StubReranker,
    StubSparse,
    make_chunk,
    make_deps,
)

pytestmark = pytest.mark.integration


def _simple_classify_resp() -> str:
    # `query_class` 不能用 `definition`：graph._after_classify 在 2026-05-27 之后
    # 把 definition 强制路由到 complex 路径（提升单 IE 定义召回）。本测试要走
    # simple fast path，必须用其它 class（procedure 最贴近"AMF 是什么"语义）。
    return json.dumps(
        {
            "query_class": "procedure",
            "complexity": "simple",
            "detected_language": "en",
            "rewritten_query": "AMF function role",
            "needs_explicit_tools": [],
            "reason": "single term",
        }
    )


def _self_rag_accept_resp() -> str:
    return json.dumps(
        {
            "faithful": True,
            "coverage": 0.9,
            "confidence": 0.88,
            "verdict": "accept",
            "missing_aspects": [],
        }
    )


async def test_astream_updates_emit_chunks_hit_after_retrieve() -> None:
    chunk = make_chunk("c1", spec_id="23.501", section=("6", "3", "1"), title="AMF")
    llm = StubLLM(
        responses=[
            _simple_classify_resp(),
            "AMF is the Access and Mobility Management Function [1].",
            _self_rag_accept_resp(),
        ]
    )
    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[chunk]),
        sparse=StubSparse(chunks=[chunk]),
        reranker=StubReranker(scores=[0.9]),
    )
    graph = build_graph(deps)

    sequence: list[tuple[str, object]] = []
    async for mode, payload in graph.astream(
        {"user_input": "What is AMF?"},
        stream_mode=["updates", "custom"],
    ):
        if mode == "updates":
            assert isinstance(payload, dict)
            # updates 模式：{node_name: {state_partial}} 单 key
            (node_name,) = payload.keys()
            sequence.append(("update", node_name))
        elif mode == "custom":
            assert isinstance(payload, dict)
            sequence.append(("custom", payload.get("type") or "<no-type>"))

    # 节点完成顺序（simple 分支）：classify → retrieve → rerank → expand → generate → self_rag
    # expand = small2big 扩段（Issue #3）；本测试 deps 无 db_sessionmaker → 透传（返回 {}），
    # 但节点仍执行，故 update 序列里出现（不改状态）。
    update_nodes = [name for kind, name in sequence if kind == "update"]
    assert update_nodes == [
        "classify",
        "retrieve",
        "rerank",
        "expand",
        "generate",
        "self_rag",
    ], f"节点 update 顺序异常：{update_nodes!r}"

    # chunks_hit 必须在 retrieve 完成事件之前 emit（writer 在节点内部、return 之前）
    custom_evts = [(i, name) for i, (kind, name) in enumerate(sequence) if kind == "custom"]
    assert any(
        name == "chunks_hit" for _, name in custom_evts
    ), f"没看到 chunks_hit 自定义事件：{sequence!r}"
    chunks_hit_idx = next(i for i, name in custom_evts if name == "chunks_hit")
    retrieve_update_idx = next(
        i for i, (kind, name) in enumerate(sequence) if kind == "update" and name == "retrieve"
    )
    assert (
        chunks_hit_idx < retrieve_update_idx
    ), f"chunks_hit (idx={chunks_hit_idx}) 应该在 retrieve update (idx={retrieve_update_idx}) 之前"


async def test_astream_events_v2_covers_all_nodes() -> None:
    chunk = make_chunk("c1", spec_id="23.501", section=("6", "3", "1"), title="AMF")
    llm = StubLLM(
        responses=[
            _simple_classify_resp(),
            "AMF is the Access and Mobility Management Function [1].",
            _self_rag_accept_resp(),
        ]
    )
    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[chunk]),
        sparse=StubSparse(chunks=[chunk]),
        reranker=StubReranker(scores=[0.9]),
    )
    graph = build_graph(deps)

    started: list[str] = []
    ended: list[str] = []
    custom_payloads: list[dict] = []

    async for evt in graph.astream_events(
        {"user_input": "What is AMF?"},
        version="v2",
    ):
        name = evt.get("name", "")
        kind = evt.get("event", "")
        if kind == "on_chain_start" and name in {
            "classify",
            "retrieve",
            "rerank",
            "expand",
            "generate",
            "self_rag",
        }:
            started.append(name)
        elif kind == "on_chain_end" and name in {
            "classify",
            "retrieve",
            "rerank",
            "expand",
            "generate",
            "self_rag",
        }:
            ended.append(name)
        elif kind == "on_custom_event":
            data = evt.get("data") or {}
            if isinstance(data, dict):
                custom_payloads.append(data)

    # 6 个节点都应有 start + end（含 small2big expand，Issue #3）
    for node in ("classify", "retrieve", "rerank", "expand", "generate", "self_rag"):
        assert node in started, f"on_chain_start 缺 {node}：{started!r}"
        assert node in ended, f"on_chain_end 缺 {node}：{ended!r}"

    # 至少 1 个 chunks_hit 自定义事件且字段符合 §7 表预期
    chunks_hit = [p for p in custom_payloads if p.get("type") == "chunks_hit"]
    assert chunks_hit, f"on_custom_event 中无 chunks_hit：{custom_payloads!r}"
    sample = chunks_hit[0]
    chunks = sample.get("chunks") or []
    assert isinstance(chunks, list) and chunks, "chunks_hit.chunks 应非空 list"
    keys = set(chunks[0].keys())
    # §7 表 payload 字段：chunk_id / spec / score / preview + content
    # （`content` 完整文本 2026-05-22 fixup-3 加，给 eval runner 拼 contexts；
    # `preview` 240 字仍保留，前端流式展示用。）
    assert {
        "chunk_id",
        "spec_id",
        "section_path",
        "score",
        "preview",
        "content",
    } <= keys, f"chunks_hit chunk payload 字段缺失：{keys!r}"


async def test_final_state_has_answer_and_citations() -> None:
    """补一条：graph 完成后的最终状态符合 §7 表 final event 期望（answer/citations/confidence）。"""
    chunk = make_chunk("c1", spec_id="23.501", section=("6", "3", "1"), title="AMF")
    llm = StubLLM(
        responses=[
            _simple_classify_resp(),
            "AMF is the Access and Mobility Management Function [1].",
            _self_rag_accept_resp(),
        ]
    )
    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[chunk]),
        sparse=StubSparse(chunks=[chunk]),
        reranker=StubReranker(scores=[0.9]),
    )
    graph = build_graph(deps)
    out = await graph.ainvoke({"user_input": "What is AMF?"})
    state = AgentState.model_validate(out)
    assert state.final_answer, "final answer 应非空"
    assert state.citations and state.citations[0]["spec_id"] == "23.501"
    assert 0 < state.confidence <= 1.0
