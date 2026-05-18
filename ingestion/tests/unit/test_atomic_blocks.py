"""atomic_blocks 单测：识别 paragraph / table / formula / figure / asn1 / action_list +
原子内切片。
"""

from __future__ import annotations

from ingestion.chunker.atomic_blocks import (
    parse_atomic_blocks,
    split_action_list_text,
    split_asn1_text,
    split_table_text,
)


def test_pure_paragraphs() -> None:
    body = "First paragraph with some text.\n\n" "Second paragraph here.\n\n" "Third paragraph."
    blocks = parse_atomic_blocks(body)
    assert len(blocks) == 1
    assert blocks[0].kind == "paragraph"
    assert "First paragraph" in blocks[0].text
    assert "Third paragraph" in blocks[0].text


def test_simple_table_recognized() -> None:
    body = (
        "Some intro text.\n\n"
        "| col1 | col2 |\n"
        "|------|------|\n"
        "| a    | b    |\n"
        "| c    | d    |\n\n"
        "Trailing prose."
    )
    blocks = parse_atomic_blocks(body)
    kinds = [b.kind for b in blocks]
    assert "table" in kinds
    table = next(b for b in blocks if b.kind == "table")
    assert "| a    | b    |" in table.text


def test_table_delim_regex_rejects_empty_cell_row() -> None:
    """`|     |     |`（全空 cell）不应被识别为 delim 行（§6.2 of 2026-05-15 POC）。

    GSMA marker 偶发把表的第一行渲染成"空 cell + 真 delim"双行结构，老正则
    `[:\\- ]+` 允许全空格 → 把空 cell 行误认为 delim → split_table_text 把后续
    pieces 的真 delim 行抛掉。
    """
    from ingestion.chunker.atomic_blocks import _TABLE_DELIM_RE

    empty_cells = "|                              |                                  |"
    assert not _TABLE_DELIM_RE.match(empty_cells)
    # 真 delim 仍要识别
    assert _TABLE_DELIM_RE.match("|---|---|")
    assert _TABLE_DELIM_RE.match("|:----|----:|")
    assert _TABLE_DELIM_RE.match("|------|------|------|")
    # 带空格的合法 delim
    assert _TABLE_DELIM_RE.match("| --- | --- |")


def test_table_empty_header_row_split_keeps_real_delim() -> None:
    """带空 cell header + 真 delim 行的表（GSMA marker 偶发产物），按行切后每片
    都应保留真 delim 行（§6.2 of 2026-05-15 POC：38.331 field descriptions 表型）。
    """
    from ingestion.chunker.atomic_blocks import split_table_text

    body_text = (
        "|        |                          |\n"
        "|--------|--------------------------|\n"
        "| field1 | description for field 1 |\n"
        "| field2 | description for field 2 |\n"
        "| field3 | description for field 3 |\n"
        "| field4 | description for field 4 |\n"
    )
    pieces = split_table_text(body_text, max_rows_per_chunk=2)
    assert len(pieces) >= 2
    for p in pieces:
        # 每片都含真 delim 行（regex 修复前第二片会丢 delim）
        has_real_delim = any("--" in ln and "|" in ln for ln in p.splitlines())
        assert has_real_delim, f"piece missing delim:\n{p}"


def test_table_with_separator_normal_recognized() -> None:
    """正常 `| h1 | h2 |\\n|---|---|\\n| v1 | v2 |` 形态：识别为 table 块。

    R4 + O5 兜底回归测三件套（M4.8 batch A.1）的对照基线：有 separator → table；
    后两测验证缺 separator 时不会被误归 table、也不会让 parse 崩。
    """
    body = "| h1 | h2 |\n|---|---|\n| v1 | v2 |\n"
    blocks = parse_atomic_blocks(body)
    kinds = [b.kind for b in blocks]
    assert kinds == ["table"]
    assert "|---|---|" in blocks[0].text


def test_table_missing_separator_falls_back_to_paragraph() -> None:
    """缺 separator（POC 38.331 §6.2 的 ~84% no-separator 路径）：不识别为 table。

    `| h1 | h2 |\\n| v1 | v2 |` 没有 `|---|` 分隔行 → atomic_blocks 不应升级为 table
    块（否则下游 split_table_text 会按行切但每片丢 delim，索引一致性塌）。预期降级
    回 paragraph，原文本完整保留。
    """
    body = "| h1 | h2 |\n| v1 | v2 |\n"
    blocks = parse_atomic_blocks(body)
    kinds = [b.kind for b in blocks]
    assert "table" not in kinds
    # 原始两行作为 paragraph 完整保留，不能被吞或重排
    joined = "\n".join(b.text for b in blocks)
    assert "| h1 | h2 |" in joined
    assert "| v1 | v2 |" in joined


def test_table_multi_row_missing_separator_stable() -> None:
    """连续多行 pipe-row 但缺 separator：兜底逻辑稳定，不抛、不升级、内容不丢。"""
    body = (
        "| h1 | h2 | h3 |\n"
        "| a1 | b1 | c1 |\n"
        "| a2 | b2 | c2 |\n"
        "| a3 | b3 | c3 |\n"
        "| a4 | b4 | c4 |\n"
    )
    blocks = parse_atomic_blocks(body)
    kinds = [b.kind for b in blocks]
    assert "table" not in kinds
    joined = "\n".join(b.text for b in blocks)
    for row in ("| a1 | b1 | c1 |", "| a4 | b4 | c4 |"):
        assert row in joined


def test_force_split_table_no_separator_falls_back_to_token_split() -> None:
    """`_force_split_table` 拿到没 separator 的表文本时退化到 `split_by_tokens`。

    section_splitter.py:227 的兜底分支：delim_idx < 0 → 不假装是表，直接 token 切；
    保证不会产出"带 header 但无 delim"的破碎片。
    """
    from ingestion.chunker.section_splitter import _force_split_table

    no_sep = "| h1 | h2 |\n" + "\n".join(f"| r{i} | v{i} |" for i in range(40))
    pieces = _force_split_table(no_sep, max_tokens=80)
    # 至少切了 2 片（40 行明显超 80 token 预算）
    assert len(pieces) >= 2
    # 没有人工编造的 `|---|` 行注入
    for p in pieces:
        assert "|---|" not in p


def test_table_with_caption_absorbed() -> None:
    body = (
        "Intro paragraph.\n\n"
        "Table 5.2.2-1: Definition of for various lengths.\n"
        "| col1 | col2 |\n"
        "|------|------|\n"
        "| a    | b    |\n"
    )
    blocks = parse_atomic_blocks(body)
    table = next(b for b in blocks if b.kind == "table")
    assert "Table 5.2.2-1" in table.text
    assert table.extra.get("caption", "").startswith("Table 5.2.2-1")
    # paragraph 末尾 caption 应被抽出，不再出现在 paragraph 块里
    paras = [b for b in blocks if b.kind == "paragraph"]
    assert all("Table 5.2.2-1" not in p.text for p in paras)


def test_asn1_block_recognized() -> None:
    body = (
        "Some intro.\n\n"
        "-- ASN1START\n"
        "Foo-r19 ::= SEQUENCE {\n"
        "    bar INTEGER\n"
        "}\n"
        "-- ASN1STOP\n\n"
        "After."
    )
    blocks = parse_atomic_blocks(body)
    kinds = [b.kind for b in blocks]
    assert "asn1" in kinds
    asn1 = next(b for b in blocks if b.kind == "asn1")
    assert asn1.text.startswith("-- ASN1START")
    assert asn1.text.endswith("-- ASN1STOP")


def test_asn1_with_example_prefix() -> None:
    body = "-- /example/ ASN1START\nA ::= INTEGER\n-- /example/ ASN1STOP"
    blocks = parse_atomic_blocks(body)
    assert len(blocks) == 1
    assert blocks[0].kind == "asn1"


def test_figure_with_gsma_description_absorbed() -> None:
    body = (
        "Some intro.\n\n"
        "![Diagram of NR architecture](abc_img.jpg)\n\n"
        "The diagram illustrates the NR architecture with UE, AMF, SMF, UPF.\n"
        "The AMF connects to UE via N1.\n\n"
        "Figure 4.2-1: Non-Roaming 5G System Architecture.\n\n"
        "Following text."
    )
    blocks = parse_atomic_blocks(body)
    fig = next(b for b in blocks if b.kind == "figure")
    assert "abc_img.jpg" in fig.text
    assert "The diagram illustrates" in fig.text
    assert "Figure 4.2-1" in fig.text
    # following text 应该是后续 paragraph
    paras = [b for b in blocks if b.kind == "paragraph"]
    assert any("Following text" in p.text for p in paras)


def test_action_list_38331_style() -> None:
    body = (
        "The UE shall:\n\n"
        "- 1> if cellGroupConfig is present:\n"
        "  - 2> apply the configuration as specified in 5.3.5.1\n"
        "  - 2> if reportType is set:\n"
        "    - 3> report the result\n"
        "- 1> else:\n"
        "  - 2> ignore.\n"
    )
    blocks = parse_atomic_blocks(body)
    kinds = [b.kind for b in blocks]
    assert "action_list" in kinds


def test_formula_block_dollar_dollar() -> None:
    body = "Intro.\n\n$$\na + b = c\n$$\n\nAfter."
    blocks = parse_atomic_blocks(body)
    formula = next(b for b in blocks if b.kind == "formula_block")
    assert "a + b = c" in formula.text


def test_image_only_no_description() -> None:
    body = "![logo](logo_img.jpg)\n"
    blocks = parse_atomic_blocks(body)
    assert blocks[0].kind == "figure"
    assert blocks[0].extra["image_path"] == "logo_img.jpg"


def test_mixed_section_with_all_kinds() -> None:
    body = (
        "# Heading inline\n\n"
        "Paragraph one.\n\n"
        "Table 1: Demo.\n"
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n\n"
        "-- ASN1START\nX ::= INTEGER\n-- ASN1STOP\n\n"
        "![pic](p_img.jpg)\nDescription paragraph.\n\n"
        "More prose.\n"
    )
    blocks = parse_atomic_blocks(body)
    kinds = {b.kind for b in blocks}
    assert {"table", "asn1", "figure", "paragraph"} <= kinds


def test_empty_body_returns_empty() -> None:
    assert parse_atomic_blocks("") == []
    assert parse_atomic_blocks("   \n\n  ") == []


# ----- 原子内切片 -----


def test_split_table_text_replicates_caption_and_header() -> None:
    table_text = "Table 5.2-1: Demo.\n" "| h1 | h2 |\n" "|----|----|\n"
    rows = "\n".join(f"| r{i} | v{i} |" for i in range(20))
    full = table_text + rows
    pieces = split_table_text(full, max_rows_per_chunk=5)
    assert len(pieces) == 4
    for p in pieces:
        assert p.startswith("Table 5.2-1: Demo.")
        assert "| h1 | h2 |" in p
        assert "|----|----|" in p


def test_split_table_text_small_returns_single() -> None:
    table_text = "| h1 | h2 |\n|----|----|\n| a | b |"
    pieces = split_table_text(table_text, max_rows_per_chunk=10)
    assert pieces == [table_text]


def test_split_asn1_text_by_top_definitions() -> None:
    asn1 = (
        "-- ASN1START\n"
        "Foo-r19 ::= SEQUENCE {\n"
        "    a INTEGER\n"
        "}\n"
        "Bar-r19 ::= SEQUENCE {\n"
        "    b INTEGER\n"
        "}\n"
        "Baz-r19 ::= SEQUENCE {\n"
        "    c INTEGER\n"
        "}\n"
        "-- ASN1STOP"
    )
    pieces = split_asn1_text(asn1)
    assert len(pieces) == 3
    for p in pieces:
        assert p.startswith("-- ASN1START")
        assert p.endswith("-- ASN1STOP")


def test_split_action_list_text_by_top_one() -> None:
    text = (
        "- 1> condition A:\n"
        "  - 2> do thing\n"
        "- 1> condition B:\n"
        "  - 2> do other thing\n"
        "  - 2> and yet another\n"
        "- 1> condition C:\n"
        "  - 2> finalize\n"
    )
    pieces = split_action_list_text(text)
    assert len(pieces) == 3
    for p in pieces:
        assert p.lstrip().startswith("- 1>")
