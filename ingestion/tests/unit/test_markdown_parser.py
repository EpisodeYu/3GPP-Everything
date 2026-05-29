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


class TestClauseLetterSuffix:
    """字母后缀 clause（`5.7a`、`5.15.11.5a` 等）必须正常解析。

    历史 bug（2026-05-29 用户复现 38.321 §5.7a/§5.7b MBS DRX）：
    `_CLAUSE_RE` 用 `[\\d.]*` 不收字母 → backtrack 全部失败 → clause 留空 →
    chunks_meta.section_path = [] → 前端 chip 标签缺 §5.7a，跳转走"未关联章节"。
    """

    def test_letter_suffix_at_tail(self):
        md = (
            "# 3GPP TS 38.321 V19.0.0\n\n"
            "## 5.7a Discontinuous Reception for MBS Broadcast\n\n"
            "Body of 5.7a.\n"
        )
        sections = parse_markdown_sections(md, spec_id="38.321", release="Rel-19")
        s = next(s for s in sections if s.clause == "5.7a")
        assert s.section_title == "Discontinuous Reception for MBS Broadcast"
        assert "Body of 5.7a." in s.body

    def test_letter_suffix_in_middle(self):
        # 真实案例（builder.py §400 注释里点过的）：5.15.11.5a
        md = (
            "# 3GPP TS 38.331 V19.0.0\n\n"
            "## 5.15.11.5a Sub-clause with mid letter suffix\n\n"
            "Body.\n"
        )
        sections = parse_markdown_sections(md, spec_id="38.331", release="Rel-19")
        s = next(s for s in sections if s.clause == "5.15.11.5a")
        assert s.section_title == "Sub-clause with mid letter suffix"

    def test_letter_suffix_followed_by_number(self):
        md = "# 3GPP TS 38.321 V19.0.0\n\n" "## 5.7a.1 First sub of 5.7a\n\n" "Body.\n"
        sections = parse_markdown_sections(md, spec_id="38.321", release="Rel-19")
        s = next(s for s in sections if s.clause == "5.7a.1")
        assert s.section_title == "First sub of 5.7a"

    def test_annex_with_letter_suffix(self):
        md = "# 3GPP TS 38.331 V19.0.0\n\n" "## B.3a Annex subsection with letter\n\n" "Body.\n"
        sections = parse_markdown_sections(md, spec_id="38.331", release="Rel-19")
        s = next(s for s in sections if s.clause == "B.3a")
        assert s.section_title == "Annex subsection with letter"

    def test_plain_clauses_still_parse(self):
        # 老路径不退化：纯数字 / annex.数字 / 多级编号都得继续工作
        md = (
            "# 3GPP TS 38.331 V19.0.0\n\n"
            "## 5 Top\n\n"
            "Body 5.\n\n"
            "## 5.1 Mid\n\n"
            "Body 5.1.\n\n"
            "## 5.1.2 Leaf\n\n"
            "Body 5.1.2.\n\n"
            "## A.1.2 Annex leaf\n\n"
            "Body A.1.2.\n"
        )
        sections = parse_markdown_sections(md, spec_id="38.331", release="Rel-19")
        clauses = {s.clause for s in sections}
        assert {"5", "5.1", "5.1.2", "A.1.2"} <= clauses

    def test_pure_text_heading_not_matched(self):
        # 不带数字的标题（Foreword / Annex A informative 等）保留为 clause=""
        md = (
            "# 3GPP TS 38.331 V19.0.0\n\n"
            "## Foreword\n\n"
            "Body.\n\n"
            "## Annex A (informative)\n\n"
            "Body annex.\n"
        )
        sections = parse_markdown_sections(md, spec_id="38.331", release="Rel-19")
        # 这俩都应当 clause="" 且 section_title 保留原文
        titles = [s.section_title for s in sections if s.clause == ""]
        assert "Foreword" in titles
        assert "Annex A (informative)" in titles

    def test_step_list_text_not_matched_as_clause(self):
        # 'next-line' regression：14a-c. 是 step 列表标号不是 section clause
        # 当它单独出现在 `##` 行时，由于 `-` 不在 clause 字符集，必须 clause=""
        # （留给上层 _is_pseudo_heading / 整段当 title）
        md = (
            "# 3GPP TS 23.502 V19.0.0\n\n"
            "## 14a-c. If the AMF has changed since registration\n\n"
            "Body.\n"
        )
        sections = parse_markdown_sections(md, spec_id="23.502", release="Rel-19")
        # 不应被识别为 clause=14a 或 14a-c
        assert all(s.clause not in {"14a", "14a-c", "14"} for s in sections)


class TestPseudoHeadingAndLongTitle:
    """覆盖 §6.5 of `2026-05-15-m1-poc-38331.md`：GSMA marker 偶发把表行误打 #### 前缀。"""

    def test_skip_pseudo_heading_starting_with_pipe(self):
        # `#### |` 后跟表格内容是 GSMA marker 偶发输出；不应被视为标题
        md = (
            "# 3GPP TS 38.331 V19.0.0\n\n"
            "## 5.1 Real section\n\n"
            "Body of 5.1.\n\n"
            "#### | <b><i>FieldName</i> descriptions</b> | |\n"
            "|---|---|\n"
            "| field1 | desc1 |\n"
        )
        sections = parse_markdown_sections(md, spec_id="38.331", release="Rel-19")
        # 不应出现 clause="" 且 title 以 `|` 起头的伪 section
        for s in sections:
            assert not s.section_title.startswith(
                "|"
            ), f"pseudo heading not filtered: title={s.section_title!r}"
        # 伪标题之后的表格内容应留在 5.1 section 的 body 内
        real = next(s for s in sections if s.clause == "5.1")
        assert "FieldName" in real.body

    def test_skip_pseudo_heading_delim_only(self):
        md = (
            "# 3GPP TS 38.331 V19.0.0\n\n"
            "## 5.1 Real section\n\n"
            "Body.\n\n"
            "### |---|---|\n"
            "| a | b |\n"
        )
        sections = parse_markdown_sections(md, spec_id="38.331", release="Rel-19")
        assert not any("|---|" in s.section_title for s in sections)

    def test_long_title_truncated(self):
        very_long = "A " * 800  # 1600 chars，必然超 1200 阈值
        md = f"# 3GPP TS 38.331 V19.0.0\n\n## 5.1 {very_long}\n\nBody.\n"
        sections = parse_markdown_sections(md, spec_id="38.331", release="Rel-19")
        s = next(s for s in sections if s.clause == "5.1")
        assert len(s.section_title) <= 1200
        assert s.section_title.endswith("…")

    def test_legitimate_long_title_preserved(self):
        """跨 2559 spec 扫描实测最长合法标题 ~1137 字符（23.502 / 33.220 procedure
        spec 的 step 标题）不应被截断。本测试用 1100 字符代表性长标题。
        """
        real_title = (
            "14a-c. If the AMF has changed since the last Registration procedure, "
            "if UE Registration type is Initial Registration or Emergency Registration, "
            "or if the UE provides a SUPI which does not refer to a valid context in "
        ) * 4  # ~900 chars，与真实 23.502 长 step 标题量级一致
        md = f"# 3GPP TS 23.502 V19.0.0\n\n#### 4.2.2.2.2 {real_title}\n\nBody.\n"
        sections = parse_markdown_sections(md, spec_id="23.502", release="Rel-19")
        s = next(s for s in sections if s.clause == "4.2.2.2.2")
        assert real_title.strip() in s.section_title
        assert not s.section_title.endswith("…")


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
