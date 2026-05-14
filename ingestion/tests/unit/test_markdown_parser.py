"""markdown_parser 单测：标题切分、clause 解析、图片引用抽取。"""

from __future__ import annotations

import pytest

from ingestion.hf_loader.markdown_parser import (
    detect_spec_type_and_title,
    extract_image_refs,
    parse_markdown_sections,
)

SAMPLE_MD = """\
# 38.211 V19.0.0 — Physical channels and modulation

(Some 3GPP preamble text here.)

## 1 Scope

The present document describes …

## 2 References

### 2.1 General

References are …

### 2.2 Normative references

![figure 2.2-1](abc_img.jpg)

Some text after the figure.

## 5 Physical channels

### 5.1 Overview

Body of overview.

### 5.2 Frame structure

#### 5.2.1 Frames

Body of 5.2.1, multiple lines.

Another paragraph.

#### 5.2.2 Slots

Body of 5.2.2.

## Annex A: Informative

Annex body.
"""


class TestParseMarkdownSections:
    def test_section_count_and_levels(self):
        sections = parse_markdown_sections(SAMPLE_MD, spec_id="38.211", release="Rel-19")
        # preamble + 10 章节 headings（H1 spec title 被跳过）
        assert len(sections) == 11
        assert sections[0].section_title == "<preamble>"
        assert sections[0].section_level == 0
        # H1 spec title 不进入 sections，第二个 section 就是 "## 1 Scope"
        assert sections[1].section_level == 2
        assert sections[1].section_title == "Scope"

    def test_clause_parsing(self):
        sections = parse_markdown_sections(SAMPLE_MD, spec_id="38.211", release="Rel-19")
        title_to_clause = {s.section_title: s.clause for s in sections}
        assert "Scope" in title_to_clause, "Scope 应该作为章节出现"
        assert title_to_clause["Scope"] == "1"
        assert title_to_clause["References"] == "2"
        assert title_to_clause["General"] == "2.1"
        assert title_to_clause["Normative references"] == "2.2"
        assert title_to_clause["Physical channels"] == "5"
        assert title_to_clause["Frame structure"] == "5.2"
        assert title_to_clause["Frames"] == "5.2.1"
        assert title_to_clause["Slots"] == "5.2.2"
        # Annex A 没有数字编号开头
        assert title_to_clause["Annex A: Informative"] == ""
        # 38.211 V19.0.0 H1 标题被跳过，所以不会出现 clause="38.211" 的污染
        assert "V19.0.0 — Physical channels and modulation" not in title_to_clause

    def test_image_refs_extracted(self):
        sections = parse_markdown_sections(SAMPLE_MD, spec_id="38.211", release="Rel-19")
        normative = next(s for s in sections if s.section_title == "Normative references")
        assert normative.image_refs == ("abc_img.jpg",)

    def test_body_chars_consistent(self):
        sections = parse_markdown_sections(SAMPLE_MD, spec_id="38.211", release="Rel-19")
        for s in sections:
            assert s.body_chars == len(s.body)

    def test_metadata_propagated(self):
        sections = parse_markdown_sections(SAMPLE_MD, spec_id="38.211", release="Rel-19")
        assert all(s.spec_id == "38.211" for s in sections)
        assert all(s.release == "Rel-19" for s in sections)

    def test_document_order_monotonic(self):
        sections = parse_markdown_sections(SAMPLE_MD, spec_id="38.211", release="Rel-19")
        orders = [s.document_order for s in sections]
        assert orders == sorted(orders)
        assert orders[0] == 0


class TestDetectSpecTypeAndTitle:
    @pytest.mark.parametrize(
        "head,spec_type,title_contains",
        [
            ("# 3GPP TS 38.211 V19.0.0 (2025-09)\nbody", "TS", "38.211"),
            ("# 3GPP TR 36.905 V18.3.0 (2023-12) ---\nbody", "TR", "36.905"),
            ("# **3GPP TR 33.737 V18.0.0**\nbody", "TR", "33.737"),
            ("# 3GPP\ttr\t38.913 V14.0.0\nbody", "TR", "38.913"),
        ],
    )
    def test_detect_standard(self, head, spec_type, title_contains):
        st, title = detect_spec_type_and_title(head)
        assert st == spec_type
        assert title is not None and title_contains in title

    def test_fallback_scans_body_for_explicit_marker(self):
        text = "# 3rd Generation Partnership Project; ...\nThis is 3GPP TR 33.737 release."
        st, _ = detect_spec_type_and_title(text)
        assert st == "TR"

    def test_study_on_heuristic_to_tr(self):
        text = "# 3rd Generation Partnership Project; Study on Vehicle-Mounted Relays"
        st, _ = detect_spec_type_and_title(text)
        assert st == "TR"

    def test_technical_specification_heuristic_to_ts(self):
        text = (
            "# 3rd Generation Partnership Project; "
            "Technical Specification Group Services and System Aspects; "
            "Description of Charge Adv"
        )
        st, _ = detect_spec_type_and_title(text)
        assert st == "TS"

    def test_spec_id_numeric_fallback(self):
        # 没有任何 hint，靠 spec_id 数字段判断
        st_low, _ = detect_spec_type_and_title("# Bare title", spec_id="22.024")
        assert st_low == "TS"
        st_high, _ = detect_spec_type_and_title("# Bare title", spec_id="22.839")
        assert st_high == "TR"

    def test_unknown_when_no_hint(self):
        st, title = detect_spec_type_and_title("# Bare title")
        assert st == "unknown"
        assert title == "Bare title"

    def test_empty(self):
        st, title = detect_spec_type_and_title("")
        assert st == "unknown"
        assert title is None


class TestExtractImageRefs:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("![](abc_img.jpg)", ["abc_img.jpg"]),
            ("plain text", []),
            ("![alt text](file1.jpg) and ![](file2.png)", ["file1.jpg", "file2.png"]),
        ],
    )
    def test_extract(self, text, expected):
        assert extract_image_refs(text) == expected
