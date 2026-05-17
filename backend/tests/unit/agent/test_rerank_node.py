"""rerank_node：调真实 reranker，失败时退回 fused 排序。"""

from __future__ import annotations

from app.agent.nodes import rerank_node
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk

from .conftest import StubReranker, make_deps


def _candidate(cid: str, *, fused: float = 0.0) -> StateChunk:
    return StateChunk(
        chunk_id=cid,
        spec_id="38.331",
        section_path=("5", "3"),
        section_title="t",
        chunk_type="text",
        content=f"content {cid}",
        fused_score=fused,
    )


async def test_rerank_reorders_by_score() -> None:
    cands = [_candidate(f"c{i}", fused=1 / (60 + i)) for i in range(4)]
    reranker = StubReranker(scores=[0.1, 0.9, 0.5, 0.3])
    deps = make_deps(reranker=reranker)
    state = AgentState(
        user_input="q",
        rewritten_queries=["english q"],
        candidates=cands,
    )

    out = await rerank_node(state, deps=deps)
    rids = [c.chunk_id for c in out["reranked"]]
    # scores: c1=0.9 / c2=0.5 / c3=0.3 / c0=0.1; settings.RERANK_TOP_K = 3
    assert rids == ["c1", "c2", "c3"]
    assert out["reranked"][0].score_rerank == 0.9


async def test_rerank_fallback_when_no_reranker() -> None:
    cands = [
        _candidate("c0", fused=0.001),
        _candidate("c1", fused=0.020),
        _candidate("c2", fused=0.010),
    ]
    deps = make_deps(reranker=None)
    state = AgentState(user_input="q", rewritten_queries=["q"], candidates=cands)

    out = await rerank_node(state, deps=deps)
    rids = [c.chunk_id for c in out["reranked"]]
    assert rids == ["c1", "c2", "c0"]


async def test_empty_candidates_returns_empty() -> None:
    deps = make_deps(reranker=StubReranker(scores=[]))
    state = AgentState(user_input="q", rewritten_queries=["q"])
    out = await rerank_node(state, deps=deps)
    assert out["reranked"] == []
