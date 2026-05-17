"""toc_tool 单测：spec/section 解析 + 子节聚合。"""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.state import AgentState
from app.tools.toc import _parse_target, toc_tool

from ..agent.conftest import make_deps
from .conftest import FakeSessionmaker


def test_parse_target_extracts_spec_and_section() -> None:
    spec, sect = _parse_target("列出 38.331 §5.3 所有子节", [])
    assert spec == "38.331"
    assert sect == ["5", "3"]


def test_parse_target_handles_just_spec() -> None:
    spec, sect = _parse_target("table of contents for 23.501", [])
    assert spec == "23.501"
    assert sect == []


def test_parse_target_section_keyword_form() -> None:
    spec, sect = _parse_target("toc of 38.331 section 5.3.5", [])
    assert spec == "38.331"
    assert sect == ["5", "3", "5"]


async def test_toc_tool_no_sessionmaker() -> None:
    deps = make_deps()
    out = await toc_tool(AgentState(user_input="38.331 §5.3"), deps=deps)
    assert out["warning"] == "db_sessionmaker unavailable"
    assert out["items"] == []


async def test_toc_tool_no_spec_id_returns_warning() -> None:
    deps = make_deps()
    deps.db_sessionmaker = FakeSessionmaker(rows=[])  # type: ignore[assignment]
    out = await toc_tool(AgentState(user_input="random text without spec"), deps=deps)
    assert out["spec_id"] is None
    assert out["warning"] == "no spec_id detected"


async def test_toc_tool_returns_sections_dedup_by_path() -> None:
    rows = [
        SimpleNamespace(
            section_path=["5", "3"],
            section_title="RRC connection",
            chunk_id="c1",
            chunk_type="text",
        ),
        SimpleNamespace(
            section_path=["5", "3"],  # 重复，应该 dedup
            section_title="RRC connection",
            chunk_id="c2",
            chunk_type="text",
        ),
        SimpleNamespace(
            section_path=["5", "3", "5"],
            section_title="RRC reconfiguration",
            chunk_id="c3",
            chunk_type="text",
        ),
    ]
    deps = make_deps()
    deps.db_sessionmaker = FakeSessionmaker(rows=rows)  # type: ignore[assignment]
    out = await toc_tool(AgentState(user_input="list 38.331 §5.3 subsections"), deps=deps)
    assert out["spec_id"] == "38.331"
    assert out["section_prefix"] == ["5", "3"]
    paths = [tuple(it["section_path"]) for it in out["items"]]
    assert paths == [("5", "3"), ("5", "3", "5")]
