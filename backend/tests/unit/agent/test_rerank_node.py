"""rerank_node：调真实 reranker，失败时退回 fused 排序。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.nodes import rerank_node
from app.agent.nodes.rerank import _definition_boost, _salient_terms
from app.agent.state import AgentState
from app.agent.state import RetrievedChunk as StateChunk
from app.core.errors import RetrievalError
from app.retrieval.models import RetrievedChunk as RetrievalChunk

from .conftest import StubReranker, make_deps, make_settings


def _candidate(
    cid: str, *, fused: float = 0.0, spec_id: str = "38.331", title: str = "t"
) -> StateChunk:
    return StateChunk(
        chunk_id=cid,
        spec_id=spec_id,
        section_path=("5", "3"),
        section_title=title,
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


class TestDefinitionBoost:
    """D：定义题专用 section_title 命中加权（_salient_terms / _definition_boost）。"""

    def test_salient_terms_extracts_ie_and_acronym(self) -> None:
        terms = _salient_terms("what is PDSCH-Config and the AMF role")
        assert "pdsch-config" in terms
        assert "amf" in terms
        # 普通小写词不算专名 token
        assert "what" not in terms

    def test_salient_terms_empty_when_no_proper_noun(self) -> None:
        assert _salient_terms("怎么 配置 这个 东西") == []

    def test_boost_promotes_title_matched_definition_chunk(self) -> None:
        # 定义条款（标题就是 IE 名）rerank 分偏低，测试规范提及分偏高；
        # boost 后定义条款应被顶进 top_k。
        chunks = [
            _b("test_hit", title="Test case 5.2.3", spec_id="38.521-4", rr=0.90),
            _b("other", title="General", spec_id="38.508-1", rr=0.68),
            _b("def", title="PDSCH-Config", spec_id="38.331", rr=0.62),
        ]
        out = _definition_boost(chunks, "PDSCH-Config 含义", weight=0.1, top_k=2)
        ids = [c.chunk_id for c in out]
        assert "def" in ids  # 0.62 + 0.1 = 0.72 > other 0.68
        assert ids[0] == "test_hit"  # 0.90 仍居首

    def test_boost_noop_without_salient_terms(self) -> None:
        chunks = [_b("a", title="x", rr=0.9), _b("b", title="y", rr=0.1)]
        out = _definition_boost(chunks, "怎么配置", weight=0.1, top_k=2)
        assert [c.chunk_id for c in out] == ["a", "b"]

    async def test_node_applies_boost_for_definition_class(self) -> None:
        # 通过 rerank_node 走一遍：query_class=definition 时放宽 pool 并应用 boost。
        cands = [
            _candidate("test_hit", title="Test case", spec_id="38.521-4"),
            _candidate("def", title="PDSCH-Config", spec_id="38.331"),
            _candidate("other", title="General", spec_id="38.508-1"),
        ]
        # Voyage 给 def 最低分；boost 后应进 top_k（RERANK_TOP_K=3）且排在 other 前
        reranker = StubReranker(scores=[0.90, 0.62, 0.68])
        deps = make_deps(reranker=reranker)
        state = AgentState(
            user_input="PDSCH-Config 是什么",
            rewritten_queries=["PDSCH-Config definition"],
            query_class="definition",
            candidates=cands,
        )
        out = await rerank_node(state, deps=deps)
        ids = [c.chunk_id for c in out["reranked"]]
        assert "def" in ids
        # pool 放宽到全部候选（top_k=len(cands)），而非默认截断
        assert reranker.calls[0]["top_k"] == len(cands)


def _b(cid: str, *, title: str, rr: float, spec_id: str = "38.331") -> StateChunk:
    return StateChunk(
        chunk_id=cid,
        spec_id=spec_id,
        section_path=("5", "3"),
        section_title=title,
        chunk_type="text",
        content=f"content {cid}",
        score_rerank=rr,
    )


# ---- map-reduce rerank 分支 ----


@dataclass
class FailingFacetReranker:
    """对指定 query 抛 RetrievalError，其余 query 按输入序返回（带降序分）。"""

    fail_on: str
    calls: list[str] = field(default_factory=list)

    async def rerank(
        self, query: str, candidates: list[RetrievalChunk], *, top_k: int = 5
    ) -> list[RetrievalChunk]:
        self.calls.append(query)
        if query == self.fail_on:
            raise RetrievalError("boom")
        out: list[RetrievalChunk] = []
        for i, c in enumerate(candidates[:top_k]):
            out.append(
                RetrievalChunk(
                    chunk_id=c.chunk_id,
                    spec_id=c.spec_id,
                    section_path=c.section_path,
                    section_title=c.section_title,
                    chunk_type=c.chunk_type,
                    content=c.content,
                    score_dense=c.score_dense,
                    score_sparse=c.score_sparse,
                    score_rerank=1.0 - i * 0.1,
                    fused_score=c.fused_score,
                    extra=dict(c.extra),
                )
            )
        return out


def _mr_settings(**kw):
    base = dict(
        RETRIEVAL_MAPREDUCE_PER_QUERY_TOPM=3,
        RETRIEVAL_MAPREDUCE_BUDGET=6,
    )
    base.update(kw)
    return make_settings(**base)


async def test_mapreduce_rerank_per_facet_and_round_robin() -> None:
    f0 = [_candidate("f0a"), _candidate("f0b")]
    f1 = [_candidate("f1a"), _candidate("f1b")]
    # scores 按输入序降序 → 每 facet top-1 = pool[0]
    reranker = StubReranker(scores=[0.9, 0.5])
    deps = make_deps(reranker=reranker, settings=_mr_settings(RETRIEVAL_MAPREDUCE_BUDGET=4))
    state = AgentState(
        user_input="q",
        rewritten_queries=["q0", "q1"],
        complexity="complex",
        query_class="procedure",
        candidates_by_query=[f0, f1],
    )

    out = await rerank_node(state, deps=deps)
    ids = [c.chunk_id for c in out["reranked"]]
    assert ids == ["f0a", "f1a", "f0b", "f1b"]  # 轮转交错
    # 每个 facet 各重排一次，且用各自的子查询
    assert len(reranker.calls) == 2
    assert {c["query"] for c in reranker.calls} == {"q0", "q1"}


async def test_mapreduce_rerank_budget_enforces_facet_fairness() -> None:
    f0 = [_candidate(f"f0_{i}") for i in range(3)]
    f1 = [_candidate("f1_0")]
    f2 = [_candidate(f"f2_{i}") for i in range(2)]
    reranker = StubReranker(scores=[0.9, 0.8, 0.7])  # 保持输入序
    deps = make_deps(reranker=reranker, settings=_mr_settings(RETRIEVAL_MAPREDUCE_BUDGET=3))
    state = AgentState(
        user_input="q",
        rewritten_queries=["q0", "q1", "q2"],
        complexity="complex",
        query_class="procedure",
        candidates_by_query=[f0, f1, f2],
    )

    out = await rerank_node(state, deps=deps)
    ids = [c.chunk_id for c in out["reranked"]]
    assert ids == ["f0_0", "f1_0", "f2_0"]  # 每 facet top-1 先于任意 facet top-2


async def test_mapreduce_rerank_no_reranker_falls_back_to_fused() -> None:
    f0 = [_candidate("f0a", fused=0.1), _candidate("f0b", fused=0.9)]
    f1 = [_candidate("f1a", fused=0.5)]
    deps = make_deps(reranker=None, settings=_mr_settings(RETRIEVAL_MAPREDUCE_BUDGET=4))
    state = AgentState(
        user_input="q",
        rewritten_queries=["q0", "q1"],
        complexity="complex",
        query_class="procedure",
        candidates_by_query=[f0, f1],
    )

    out = await rerank_node(state, deps=deps)
    ids = [c.chunk_id for c in out["reranked"]]
    # facet0 fused 降序 f0b>f0a；facet1 f1a；轮转 → f0b, f1a, f0a
    assert ids == ["f0b", "f1a", "f0a"]


async def test_mapreduce_rerank_facet_failure_isolated() -> None:
    f0 = [_candidate("f0a")]
    f1 = [_candidate("f1a", fused=0.7)]
    reranker = FailingFacetReranker(fail_on="q1")
    deps = make_deps(reranker=reranker, settings=_mr_settings())  # type: ignore[arg-type]
    state = AgentState(
        user_input="q",
        rewritten_queries=["q0", "q1"],
        complexity="complex",
        query_class="procedure",
        candidates_by_query=[f0, f1],
    )

    out = await rerank_node(state, deps=deps)
    ids = {c.chunk_id for c in out["reranked"]}
    assert ids == {"f0a", "f1a"}  # q1 facet rerank 失败但退回 fused，f1a 仍在
