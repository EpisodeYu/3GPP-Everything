"""术语抽取单测（M4.1）。

覆盖：
- normalize_term 大小写 / 空白折叠
- extract_bold_definitions：单条 / 多条 / 跨段落 / 紧贴下一级标题
- extract_abbreviations：标准表 / 头部分隔行 / 紧凑 `MA PDU` 形式
- extract_from_sections：标题白名单分发（含合并标题）
- PgGlossaryWriter：DELETE-then-INSERT 幂等 + 同 spec 同 normalized_term 去重 + 跨 spec 不去重
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from ingestion.glossary.extractor import (
    ABBREVIATION_SECTION_TITLES,
    DEFINITION_SECTION_TITLES,
    GlossaryEntry,
    extract_abbreviations,
    extract_bold_definitions,
    extract_from_sections,
    normalize_term,
)
from ingestion.glossary.writer import PgGlossaryWriter

pytestmark = pytest.mark.unit


# ----------------------- normalize_term -----------------------


def test_normalize_term_lowercases_and_strips():
    assert normalize_term("  PDU Session ") == "pdu session"
    assert normalize_term("AMF") == "amf"
    assert normalize_term("'5G-GUTI'") == "5g-guti"


def test_normalize_term_collapses_internal_whitespace():
    assert normalize_term("PDU\tSession") == "pdu session"
    assert normalize_term("MA  PDU") == "ma pdu"
    assert normalize_term("5G\n System") == "5g system"


# ----------------------- bold definitions -----------------------


def test_extract_bold_definitions_simple_pair():
    text = "**5GS:** 5G System.\n" "\n" "**AMF:** Access and Mobility Management Function.\n"
    out = extract_bold_definitions(text, "24.501", ["3.1"], source_revision="rev1")
    assert [e.term for e in out] == ["5GS", "AMF"]
    assert out[0].definition == "5G System."
    assert out[0].normalized_term == "5gs"
    assert out[0].spec_id == "24.501"
    assert out[0].section_path == ["3.1"]
    assert out[0].source_revision == "rev1"
    assert out[1].definition == "Access and Mobility Management Function."


def test_extract_bold_definitions_multiparagraph_until_next_term():
    text = (
        "**Access stratum connection:** A peer to peer access stratum connection:\n"
        "\n"
        "- between the UE and the NG-RAN for 3GPP access;\n"
        "- between the UE and the N3IWF for untrusted non-3GPP access;\n"
        "\n"
        "Additional clarifying paragraph.\n"
        "\n"
        "**Aggregate maximum bit rate:** The maximum bit rate.\n"
    )
    out = extract_bold_definitions(text, "24.501", ["3.1"])
    terms = [e.term for e in out]
    assert terms == ["Access stratum connection", "Aggregate maximum bit rate"]
    asc = out[0]
    assert "between the UE and the NG-RAN" in asc.definition
    assert "Additional clarifying paragraph." in asc.definition
    # 终止：未泄漏到下一条 term
    assert "Aggregate maximum bit rate" not in asc.definition


def test_extract_bold_definitions_stops_at_next_heading():
    text = (
        "**PDU Session:** Association between UE and DN.\n"
        "\n"
        "## 3.2 Abbreviations\n"
        "\n"
        "| AMF | Access and Mobility Management Function |\n"
    )
    out = extract_bold_definitions(text, "24.501", ["3.1"])
    assert len(out) == 1
    assert out[0].term == "PDU Session"
    assert "AMF" not in out[0].definition
    assert "Abbreviations" not in out[0].definition


def test_extract_bold_definitions_ignores_inline_bold():
    text = (
        "**Term1:** definition with **emphasis** in the middle and trailing colon: stuff.\n"
        "\n"
        "**Term2:** other definition.\n"
    )
    out = extract_bold_definitions(text, "24.501", ["3.1"])
    assert [e.term for e in out] == ["Term1", "Term2"]
    assert "**emphasis**" in out[0].definition


def test_extract_bold_definitions_empty_body_skipped():
    text = "**Term:**\n\n**Other:** value\n"
    out = extract_bold_definitions(text, "X.Y", ["3.1"])
    assert [e.term for e in out] == ["Other"]


# ----------------------- abbreviations -----------------------


def test_extract_abbreviations_skips_separator_and_header():
    text = (
        "|           |                                       |\n"
        "|-----------|---------------------------------------|\n"
        "| 5GS       | 5G System                             |\n"
        "| AMF       | Access and Mobility Management Function |\n"
        "| MA PDU    | Multi-Access PDU                      |\n"
    )
    out = extract_abbreviations(text, "24.501", ["3.2"])
    terms = [e.term for e in out]
    assert terms == ["5GS", "AMF", "MA PDU"]
    amf = next(e for e in out if e.term == "AMF")
    assert amf.definition == "Access and Mobility Management Function"
    assert amf.normalized_term == "amf"
    ma_pdu = next(e for e in out if e.term == "MA PDU")
    assert ma_pdu.normalized_term == "ma pdu"


def test_extract_abbreviations_drops_overlong_col1():
    text = (
        "| " + ("X" * 80) + " | suspicious very long abbreviation column |\n"
        "| AMF | Access and Mobility Management Function |\n"
    )
    out = extract_abbreviations(text, "24.501", ["3.2"])
    assert [e.term for e in out] == ["AMF"]


# ----------------------- section dispatch -----------------------


def test_dispatch_titles_cover_canonical_variants():
    # 关键白名单条目都得在 set 中（防 typo 回归）
    for title in ["definitions", "definitions and abbreviations"]:
        assert title in DEFINITION_SECTION_TITLES
    for title in ["abbreviations", "definitions and abbreviations"]:
        assert title in ABBREVIATION_SECTION_TITLES


def _section(clause: str, title: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(clause=clause, section_title=title, body=body)


def test_extract_from_sections_dispatches_by_title():
    sections = [
        _section("3.1", "Definitions", "**AMF:** Access and Mobility Management Function.\n"),
        _section(
            "3.2",
            "Abbreviations",
            "|     |     |\n|-----|-----|\n| 5GS | 5G System |\n",
        ),
        # 非白名单标题不抽取
        _section("4", "General", "**Foo:** Bar baz.\n"),
    ]
    out = extract_from_sections(sections, spec_id="24.501")
    terms = [e.term for e in out]
    assert "AMF" in terms
    assert "5GS" in terms
    assert "Foo" not in terms


def test_extract_from_sections_combined_title_runs_both():
    sections = [
        _section(
            "3",
            "Definitions and abbreviations",
            "**AMF:** Access and Mobility Management Function.\n"
            "\n"
            "|     |     |\n|-----|-----|\n| 5GS | 5G System |\n",
        ),
    ]
    out = extract_from_sections(sections, spec_id="24.501", source_revision="rev42")
    terms = [e.term for e in out]
    assert "AMF" in terms
    assert "5GS" in terms
    for entry in out:
        assert entry.source_revision == "rev42"
        assert entry.section_path == ["3"]


def test_extract_from_sections_falls_back_to_title_when_clause_empty():
    sections = [
        _section("", "Definitions", "**X:** Y.\n"),
    ]
    out = extract_from_sections(sections, spec_id="X.Y")
    assert out[0].section_path == ["Definitions"]


# ----------------------- PgGlossaryWriter -----------------------


def _make_writer(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/glossary.db")
    return PgGlossaryWriter(engine=engine, schema_owner=True)


def _entry(
    term: str,
    *,
    spec_id: str = "24.501",
    section_path: list[str] | None = None,
) -> GlossaryEntry:
    return GlossaryEntry(
        term=term,
        normalized_term=normalize_term(term),
        definition=f"def-of-{term}",
        spec_id=spec_id,
        section_path=section_path or ["3.2"],
        source_revision="rev1",
    )


def test_writer_upsert_is_idempotent(tmp_path):
    writer = _make_writer(tmp_path)
    entries = [_entry("AMF"), _entry("SMF"), _entry("UPF")]
    assert writer.upsert_spec("24.501", entries) == 3
    # 第二次写入同样 3 条；DELETE 老的，INSERT 新的，row 数稳定为 3
    assert writer.upsert_spec("24.501", entries) == 3
    assert writer.count() == 3
    assert writer.count(spec_id="24.501") == 3


def test_writer_dedupes_within_batch(tmp_path):
    writer = _make_writer(tmp_path)
    entries = [
        _entry("AMF"),
        _entry("amf"),  # 同 normalized + 同 section_path → 去重
        _entry("SMF"),
    ]
    assert writer.upsert_spec("24.501", entries) == 2
    assert writer.count() == 2


def test_writer_keeps_cross_spec_duplicates(tmp_path):
    writer = _make_writer(tmp_path)
    writer.upsert_spec("24.501", [_entry("AMF", spec_id="24.501")])
    writer.upsert_spec("23.501", [_entry("AMF", spec_id="23.501")])
    assert writer.count() == 2
    assert writer.count(spec_id="24.501") == 1
    assert writer.count(spec_id="23.501") == 1
    rows = writer.find_by_normalized("amf")
    assert {r["spec_id"] for r in rows} == {"24.501", "23.501"}


def test_writer_upsert_empty_purges_existing(tmp_path):
    writer = _make_writer(tmp_path)
    writer.upsert_spec("24.501", [_entry("AMF"), _entry("SMF")])
    assert writer.count(spec_id="24.501") == 2
    assert writer.upsert_spec("24.501", []) == 0
    assert writer.count(spec_id="24.501") == 0


def test_writer_find_by_normalized_returns_full_row(tmp_path):
    writer = _make_writer(tmp_path)
    writer.upsert_spec(
        "24.501",
        [
            GlossaryEntry(
                term="PDU Session",
                normalized_term="pdu session",
                definition="Association between UE and DN.",
                spec_id="24.501",
                section_path=["3.1"],
                source_revision="rev42",
            )
        ],
    )
    rows = writer.find_by_normalized("pdu session")
    assert len(rows) == 1
    row = rows[0]
    assert row["term"] == "PDU Session"
    assert row["definition"] == "Association between UE and DN."
    assert row["spec_id"] == "24.501"
    assert row["section_path"] == ["3.1"]
    assert row["source_revision"] == "rev42"
