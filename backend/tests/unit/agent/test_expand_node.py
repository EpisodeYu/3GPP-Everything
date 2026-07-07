"""expand_node（small2big 召回侧扩段，Issue #3）单测。

覆盖：开关 / 无 db / 空 reranked / 无 parent 透传；整段扩展；同 parent 去重；
退化窗口；全局字符预算截断；Qdrant 空 / PG 异常降级。

stub 约定：
- FakeSessionmaker：`sm()` 返回一个 async 上下文 session，`execute(stmt)` 无视 WHERE，
  直接吐预置的 sibling rows（node 的分组 / 排序 / 窗口逻辑本身才是被测对象）。
- StubDense.content_map：模拟 Qdrant 按 chunk_id 取 content。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.nodes import expand_node
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk

from .conftest import StubDense, make_deps, make_settings


@dataclass
class FakeRow:
    """最小 chunks_meta 行：expand 只读这四个字段。"""

    chunk_id: str
    parent_section_id: str
    document_order: int
    parent_section_chars: int


class _FakeResult:
    def __init__(self, rows: list[FakeRow]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[FakeRow]:
        return list(self._rows)


@dataclass
class _FakeSession:
    rows: list[FakeRow]
    raise_on_execute: bool = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, _stmt: Any) -> _FakeResult:
        if self.raise_on_execute:
            raise RuntimeError("pg boom")
        return _FakeResult(self.rows)


@dataclass
class FakeSessionmaker:
    rows: list[FakeRow] = field(default_factory=list)
    raise_on_execute: bool = False

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.rows, self.raise_on_execute)


def _chunk(
    cid: str, *, parent: str | None = "P1", section: tuple[str, ...] = ("5", "3")
) -> StateChunk:
    extra = {"parent_section_id": parent} if parent else {}
    return StateChunk(
        chunk_id=cid,
        spec_id="38.331",
        section_path=section,
        section_title="RRC",
        chunk_type="text",
        content=f"small-{cid}",
        score_rerank=1.0,
        extra=extra,
    )


def _rows(*specs: tuple[str, str, int, int]) -> list[FakeRow]:
    return [FakeRow(cid, pid, order, chars) for cid, pid, order, chars in specs]


# === 透传 / 早退 =========================================================


async def test_disabled_returns_empty() -> None:
    deps = make_deps(
        dense=StubDense(chunks=[], content_map={"a": "x"}),
        db_sessionmaker=FakeSessionmaker(rows=_rows(("a", "P1", 0, 10))),
        settings=make_settings(SMALL2BIG_ENABLED=False),
    )
    state = AgentState(user_input="q", reranked=[_chunk("a")])
    assert await expand_node(state, deps=deps) == {}


async def test_no_db_returns_empty() -> None:
    deps = make_deps(dense=StubDense(chunks=[], content_map={"a": "x"}), db_sessionmaker=None)
    state = AgentState(user_input="q", reranked=[_chunk("a")])
    assert await expand_node(state, deps=deps) == {}


async def test_empty_reranked_returns_empty() -> None:
    deps = make_deps(db_sessionmaker=FakeSessionmaker())
    state = AgentState(user_input="q", reranked=[])
    assert await expand_node(state, deps=deps) == {}


async def test_no_parent_id_returns_empty() -> None:
    deps = make_deps(
        dense=StubDense(chunks=[], content_map={"a": "x"}),
        db_sessionmaker=FakeSessionmaker(rows=_rows(("a", "P1", 0, 10))),
    )
    state = AgentState(user_input="q", reranked=[_chunk("a", parent=None)])
    assert await expand_node(state, deps=deps) == {}


# === 正常扩段 ============================================================


async def test_expands_full_section_in_document_order() -> None:
    # 命中块 a（P1）；整段 3 块，document_order 打乱验证节点会重排。
    rows = _rows(("c", "P1", 2, 30), ("a", "P1", 0, 30), ("b", "P1", 1, 30))
    content = {"a": "AAA", "b": "BBB", "c": "CCC"}
    deps = make_deps(
        dense=StubDense(chunks=[], content_map=content),
        db_sessionmaker=FakeSessionmaker(rows=rows),
        settings=make_settings(
            SMALL2BIG_MAX_SECTION_CHARS=10_000, SMALL2BIG_TOTAL_BUDGET_CHARS=10_000
        ),
    )
    state = AgentState(user_input="q", reranked=[_chunk("a")])

    out = await expand_node(state, deps=deps)
    reranked = out["reranked"]
    assert len(reranked) == 1
    # 按 document_order a(0) → b(1) → c(2) 拼接
    assert reranked[0].expanded_content == "AAA\n\nBBB\n\nCCC"


async def test_dedup_same_parent_only_top_ranked_expands() -> None:
    # a、b 都属 P1，只有名次最高的 a 被扩；b 保留小块。
    rows = _rows(("a", "P1", 0, 30), ("b", "P1", 1, 30))
    deps = make_deps(
        dense=StubDense(chunks=[], content_map={"a": "AAA", "b": "BBB"}),
        db_sessionmaker=FakeSessionmaker(rows=rows),
        settings=make_settings(
            SMALL2BIG_MAX_SECTION_CHARS=10_000, SMALL2BIG_TOTAL_BUDGET_CHARS=10_000
        ),
    )
    state = AgentState(user_input="q", reranked=[_chunk("a"), _chunk("b")])

    out = await expand_node(state, deps=deps)
    reranked = out["reranked"]
    assert reranked[0].expanded_content == "AAA\n\nBBB"
    assert reranked[1].expanded_content == ""  # 同 parent 的次名块不重复扩


async def test_degrade_to_neighbor_window_when_section_too_large() -> None:
    # 7 兄弟块，parent_section_chars 超阈值 → 退化到命中块前后各 N=1。
    rows = _rows(*[(f"s{i}", "P1", i, 999_999) for i in range(7)])
    content = {f"s{i}": f"C{i}" for i in range(7)}
    deps = make_deps(
        dense=StubDense(chunks=[], content_map=content),
        db_sessionmaker=FakeSessionmaker(rows=rows),
        settings=make_settings(
            SMALL2BIG_MAX_SECTION_CHARS=10,
            SMALL2BIG_NEIGHBOR_WINDOW=1,
            SMALL2BIG_TOTAL_BUDGET_CHARS=10_000,
        ),
    )
    # 命中块 = s3（中间）
    hit = _chunk("s3")
    state = AgentState(user_input="q", reranked=[hit])

    out = await expand_node(state, deps=deps)
    # window [s2, s3, s4]
    assert out["reranked"][0].expanded_content == "C2\n\nC3\n\nC4"


async def test_global_budget_stops_later_expansions() -> None:
    # 两个 parent；预算只够第一个整段，第二个不再扩。
    rows = _rows(("a", "P1", 0, 30), ("b", "P2", 5, 30))
    deps = make_deps(
        dense=StubDense(chunks=[], content_map={"a": "AAAAA", "b": "BBBBB"}),
        db_sessionmaker=FakeSessionmaker(rows=rows),
        settings=make_settings(
            SMALL2BIG_MAX_SECTION_CHARS=10_000,
            SMALL2BIG_TOTAL_BUDGET_CHARS=5,  # 只够 "AAAAA"
        ),
    )
    state = AgentState(
        user_input="q",
        reranked=[_chunk("a", parent="P1"), _chunk("b", parent="P2")],
    )

    out = await expand_node(state, deps=deps)
    reranked = out["reranked"]
    assert reranked[0].expanded_content == "AAAAA"
    assert reranked[1].expanded_content == ""  # 预算耗尽，靠后的 parent 不扩


async def test_budget_truncates_boundary_chunk() -> None:
    rows = _rows(("a", "P1", 0, 30))
    deps = make_deps(
        dense=StubDense(chunks=[], content_map={"a": "ABCDEFGHIJ"}),
        db_sessionmaker=FakeSessionmaker(rows=rows),
        settings=make_settings(SMALL2BIG_MAX_SECTION_CHARS=10_000, SMALL2BIG_TOTAL_BUDGET_CHARS=4),
    )
    state = AgentState(user_input="q", reranked=[_chunk("a")])
    out = await expand_node(state, deps=deps)
    assert out["reranked"][0].expanded_content == "ABCD"  # 截到剩余预算


# === 降级（不阻塞主路径）=================================================


async def test_qdrant_empty_returns_empty() -> None:
    rows = _rows(("a", "P1", 0, 30))
    deps = make_deps(
        dense=StubDense(chunks=[], content_map={}),  # Qdrant 取不到 content
        db_sessionmaker=FakeSessionmaker(rows=rows),
    )
    state = AgentState(user_input="q", reranked=[_chunk("a")])
    assert await expand_node(state, deps=deps) == {}


async def test_pg_error_returns_empty() -> None:
    deps = make_deps(
        dense=StubDense(chunks=[], content_map={"a": "AAA"}),
        db_sessionmaker=FakeSessionmaker(rows=_rows(("a", "P1", 0, 30)), raise_on_execute=True),
    )
    state = AgentState(user_input="q", reranked=[_chunk("a")])
    assert await expand_node(state, deps=deps) == {}
