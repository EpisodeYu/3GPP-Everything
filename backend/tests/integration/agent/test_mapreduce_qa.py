"""Map-reduce 检索（A 范式）端到端（mock graph）。

口径：`docs/04-handoff/2026-06-02-mapreduce-retrieval-plan.md`。
- flag 开 + classify=complex/procedure → 走 rewrite→hyde→multi_query→retrieve→rerank
- multi_query 产出多条子查询 → retrieve 走 map-reduce，每子查询独立候选池
- rerank 每 facet 用各自子查询重排 + 轮转合并 → reranked 含多个不同 facet 的证据
- 对照：flag 关时同输入只产出单池 reranked（candidates_by_query 为空）
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from app.agent.graph import build_graph
from app.agent.state import AgentState
from app.retrieval.models import RetrievedChunk as RetrievalChunk

from ...unit.agent.conftest import StubLLM, StubReranker, make_chunk, make_deps, make_settings

pytestmark = pytest.mark.integration


@dataclass
class SequencedDense:
    """按调用次序返回预置 chunk 列表的 dense stub（与 query 字符串解耦）。

    map-reduce retrieve 的 dense 调用是串行的：facet0, facet1, …, 最后 hyde。
    """

    per_call: list[list[RetrievalChunk]]
    calls: list[str] = field(default_factory=list)

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 30,
        filter_spec_ids: Sequence[str] | None = None,
    ) -> list[RetrievalChunk]:
        i = len(self.calls)
        self.calls.append(query)
        chunks = self.per_call[i] if i < len(self.per_call) else []
        return list(chunks)[:top_k]

    async def close(self) -> None:
        pass


_CLASSIFY = json.dumps(
    {
        "query_class": "procedure",
        "complexity": "complex",
        "detected_language": "en",
        "rewritten_query": "RRC connection setup procedure",
        "needs_explicit_tools": [],
        "reason": "multi-entity multi-section",
    }
)
_REWRITE = "RRC connection setup detailed steps"
_HYDE = "RRC connection setup involves SRB establishment and AMF registration ..."
_MULTI_QUERY = json.dumps(["AMF registration procedure", "NGAP initial UE message"])
_GENERATE = "RRC setup spans RAN and core [1][2]."
_SELF_RAG_ACCEPT = json.dumps(
    {
        "faithful": True,
        "coverage": 0.9,
        "confidence": 0.9,
        "verdict": "accept",
        "missing_aspects": [],
    }
)


def _llm() -> StubLLM:
    return StubLLM(
        responses=[_CLASSIFY, _REWRITE, _HYDE, _MULTI_QUERY, _GENERATE, _SELF_RAG_ACCEPT]
    )


async def test_mapreduce_end_to_end_yields_multi_facet_reranked() -> None:
    # 三个 facet 各召回不同 spec 的 chunk；hyde 那次 dense 调用返回空
    facet_a = make_chunk("a1", spec_id="38.331", title="RRC connection establishment")
    facet_b = make_chunk("b1", spec_id="23.501", title="Registration management")
    facet_c = make_chunk("c1", spec_id="38.413", title="Initial UE message")
    dense = SequencedDense(per_call=[[facet_a], [facet_b], [facet_c], []])
    deps = make_deps(
        llm=_llm(),
        dense=dense,  # type: ignore[arg-type]
        sparse=None,
        reranker=StubReranker(scores=[0.95]),
        settings=make_settings(RETRIEVAL_MAPREDUCE_ENABLED=True),
    )
    graph = build_graph(deps)

    out = await graph.ainvoke({"user_input": "How does RRC connection setup work end to end?"})
    state = AgentState.model_validate(out)

    # 走了 map-reduce：3 个 facet 候选池
    assert len(state.candidates_by_query) == 3
    # reranked 含 ≥2 个不同 spec 的证据（轮转合并保住弱 facet）
    specs = {c.spec_id for c in state.reranked}
    assert len(specs) >= 2
    assert {"38.331", "23.501", "38.413"} <= specs
    assert state.final_answer
    assert state.self_rag_verdict == "accept"


async def test_flag_off_same_input_uses_single_pool() -> None:
    facet_a = make_chunk("a1", spec_id="38.331")
    facet_b = make_chunk("b1", spec_id="23.501")
    facet_c = make_chunk("c1", spec_id="38.413")
    dense = SequencedDense(per_call=[[facet_a], [facet_b], [facet_c], []])
    deps = make_deps(
        llm=_llm(),
        dense=dense,  # type: ignore[arg-type]
        sparse=None,
        reranker=StubReranker(scores=[0.95, 0.9, 0.85]),
        settings=make_settings(RETRIEVAL_MAPREDUCE_ENABLED=False),
    )
    graph = build_graph(deps)

    out = await graph.ainvoke({"user_input": "How does RRC connection setup work end to end?"})
    state = AgentState.model_validate(out)

    # single-pool 路径：不产出 per-facet 候选池
    assert state.candidates_by_query == []
    assert state.final_answer
