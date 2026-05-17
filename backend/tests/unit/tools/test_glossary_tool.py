"""glossary_tool 单测。"""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.state import AgentState
from app.tools.glossary import _extract_candidates, glossary_tool

from ..agent.conftest import make_deps
from .conftest import FakeSessionmaker


def test_extract_candidates_picks_abbrev_and_skips_stopwords() -> None:
    cands = _extract_candidates("What is AMF? Describe PDU Session.", [])
    # AMF / PDU / Session 都应入选；What / is / Describe 是 stopword 被滤
    upper = [c.upper() for c in cands]
    assert "AMF" in upper
    assert "PDU" in upper
    assert "SESSION" in upper
    assert "WHAT" not in upper
    assert "IS" not in upper


def test_extract_candidates_capped_and_deduped() -> None:
    cands = _extract_candidates("AMF AMF SMF UPF N1 N2 N3 N4 N5 N6 N7", [])
    # 去重 + cap 8
    assert len(cands) <= 8
    assert len({c.upper() for c in cands}) == len(cands)


async def test_glossary_tool_no_sessionmaker_returns_warning() -> None:
    deps = make_deps()  # db_sessionmaker=None
    out = await glossary_tool(AgentState(user_input="What is AMF?"), deps=deps)
    assert out["matches"] == []
    assert out["warning"] == "db_sessionmaker unavailable"


async def test_glossary_tool_no_candidates_returns_empty() -> None:
    deps = make_deps()
    deps.db_sessionmaker = FakeSessionmaker(rows=[])  # type: ignore[assignment]
    out = await glossary_tool(AgentState(user_input="the of a"), deps=deps)
    assert out["matches"] == []
    assert out["warning"] is None


async def test_glossary_tool_hits_rows_and_normalizes_payload() -> None:
    row = SimpleNamespace(
        term="AMF",
        normalized_term="amf",
        definition="Access and Mobility Management Function",
        spec_id="23.501",
        section_path=["3", "1"],
    )
    deps = make_deps()
    deps.db_sessionmaker = FakeSessionmaker(rows=[row])  # type: ignore[assignment]
    out = await glossary_tool(AgentState(user_input="What is AMF?"), deps=deps)
    assert len(out["matches"]) == 1
    m = out["matches"][0]
    assert m["term"] == "AMF"
    assert m["spec_id"] == "23.501"
    assert m["section_path"] == ["3", "1"]


async def test_glossary_tool_db_error_returns_warning() -> None:
    deps = make_deps()
    deps.db_sessionmaker = FakeSessionmaker(raises=RuntimeError("conn refused"))  # type: ignore[assignment]
    out = await glossary_tool(AgentState(user_input="What is AMF?"), deps=deps)
    assert out["matches"] == []
    assert "conn refused" in (out["warning"] or "")
