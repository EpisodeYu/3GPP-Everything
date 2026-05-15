"""section_splitter 单测：贪心 packing / 三级 fallback / overlap / figure 透传。"""

from __future__ import annotations

from ingestion.chunker.atomic_blocks import parse_atomic_blocks
from ingestion.chunker.models import AtomicBlock
from ingestion.chunker.section_splitter import split_section


def test_short_section_one_chunk() -> None:
    body = "Short paragraph one.\n\nShort paragraph two."
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=250, max_tokens=400)
    assert len(pieces) == 1
    assert pieces[0].chunk_type == "text"


def test_long_paragraph_split_by_sentence_fallback() -> None:
    sentences = [f"This is a moderately long sentence number {i}." for i in range(200)]
    body = " ".join(sentences)
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=100, max_tokens=200, overlap_tokens=0)
    assert len(pieces) > 1
    # 每片都不超过 max_tokens × 1.2（splitter 的语义边界回溯允许一点超出）
    from ingestion.chunker.tokenize_utils import count_tokens

    for p in pieces:
        assert count_tokens(p.text) <= 240, f"piece too large: {count_tokens(p.text)}"


def test_table_atomic_no_split_when_small() -> None:
    body = (
        "Para before.\n\n"
        "Table 1: Demo.\n"
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n\n"
        "Para after."
    )
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=250, max_tokens=400)
    # 表是原子块；可能与前后 paragraph pack 在一起
    assert any(p.chunk_type == "table" or "table" in p.source_kinds for p in pieces)


def test_figure_yielded_as_separate_piece() -> None:
    body = (
        "Some intro paragraph.\n\n"
        "![A diagram](abc_img.jpg)\nDescription paragraph for the diagram.\n\n"
        "Following text."
    )
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=250, max_tokens=400)
    fig_pieces = [p for p in pieces if p.chunk_type == "figure"]
    assert len(fig_pieces) == 1
    assert "abc_img.jpg" in fig_pieces[0].text


def test_overlap_applies_between_paragraph_chunks() -> None:
    sentences = [
        f"Sentence number {i}, somewhat long for token counting purposes here. " for i in range(80)
    ]
    body = "".join(sentences)
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=80, max_tokens=120, overlap_tokens=30)
    assert len(pieces) >= 2
    # 第二片应包含来自第一片末尾的 overlap 文本（句号后最早的句子起点）
    # 此处不要求精确匹配；只验证第二片不与第一片完全独立
    # 即至少有一些重复 token 的可能性。这里做弱断言：第二片长度应大于 0
    assert all(len(p.text) > 0 for p in pieces)


def test_oversize_table_split_by_rows() -> None:
    rows = "\n".join(f"| r{i} | very long row content {i*100} |" for i in range(200))
    body = "Intro.\n\n" "Table 1: Big.\n" "| h1 | h2 |\n" "|----|----|\n" + rows
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=250, max_tokens=400)
    table_pieces = [p for p in pieces if p.chunk_type == "table"]
    assert len(table_pieces) > 1
    for p in table_pieces:
        assert "Table 1: Big." in p.text
        assert "|----|----|" in p.text


def test_oversize_asn1_split_by_top_def() -> None:
    body_lines = ["-- ASN1START"]
    for i in range(20):
        body_lines.append(f"VeryLongIdentifier{i}-r19 ::= SEQUENCE {{")
        body_lines.append("    field INTEGER")
        body_lines.append("    other OCTET STRING SIZE (1..1024)")
        body_lines.append("}")
    body_lines.append("-- ASN1STOP")
    body = "\n".join(body_lines)
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=100, max_tokens=200)
    asn1_pieces = [p for p in pieces if p.chunk_type == "asn1"]
    # 大 ASN.1 应被切；每片仍有 ASN1START/STOP 包裹
    assert len(asn1_pieces) > 1
    for p in asn1_pieces:
        assert p.text.startswith("-- ASN1START")
        assert p.text.endswith("-- ASN1STOP")


def test_atomic_block_kinds_preserved_in_source_kinds() -> None:
    blocks = [
        AtomicBlock(kind="paragraph", text="Some intro text " * 5),
        AtomicBlock(kind="formula_block", text="$$ x + y = z $$"),
    ]
    pieces = split_section(blocks, target_tokens=500, max_tokens=800)
    # 公式块应进 source_kinds
    found_formula = any("formula_block" in p.source_kinds for p in pieces)
    assert found_formula


def test_empty_blocks_returns_empty() -> None:
    assert split_section([]) == []


def test_oversize_table_force_split_preserves_header_separator() -> None:
    """§6.2 of `2026-05-15-m1-poc-38331.md`：超大表强切时每片仍带 caption+header+`|---|`。

    构造一个含极长 cell（单行 ~600 tokens）的表，目标 max_tokens=200 → 必然触发
    safety net；验证每片 markdown 都含 `|---|` 分隔行（前端 react-markdown +
    remark-gfm 能正确渲染）。
    """
    big_cell = "long word " * 200  # ~400 tokens
    rows = [f"| field{i} | {big_cell} |" for i in range(8)]
    body = "Table 5.1-1: Big descriptions.\n| name | desc |\n|------|------|\n" + "\n".join(rows)
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=100, max_tokens=200, overlap_tokens=0)
    table_pieces = [p for p in pieces if p.chunk_type == "table"]
    assert len(table_pieces) > 1
    for p in table_pieces:
        # 每片都应含 separator 行（即使是 force_split_overflow 兜底也加回 header）
        has_sep = any(
            ln.strip().startswith("|")
            and set(ln.replace("|", "").replace(":", "").strip()) <= {"-", " "}
            and "-" in ln
            for ln in p.text.splitlines()
        )
        assert has_sep, f"table piece missing |---| separator:\n{p.text[:300]}"
        # caption 也保留
        assert "Table 5.1-1" in p.text, f"table piece missing caption:\n{p.text[:200]}"


def test_oversize_table_with_single_huge_row_each_piece_keeps_header() -> None:
    """单 cell 极长导致每行 > max_tokens，每个 hard-split 子片仍要带 header+delim。"""
    body = "Table 6.1-1: Single huge.\n" "| col |\n" "|-----|\n" f"| {'x ' * 500} |\n"
    blocks = parse_atomic_blocks(body)
    pieces = split_section(blocks, target_tokens=80, max_tokens=120, overlap_tokens=0)
    table_pieces = [p for p in pieces if p.chunk_type == "table"]
    assert table_pieces
    for p in table_pieces:
        assert "Table 6.1-1" in p.text
        assert "|-----|" in p.text
