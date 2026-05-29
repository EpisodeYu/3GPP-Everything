"""garbage_filter 单测：stop-list / 启发式 / 边界情况。"""

from __future__ import annotations

from ingestion.chunker.garbage_filter import filter_sections, is_garbage
from ingestion.hf_loader.models import SectionBlock


def _sec(
    *,
    title: str = "Section",
    body: str = "x" * 100,
    clause: str = "1.1",
    level: int = 2,
    document_order: int = 1,
) -> SectionBlock:
    return SectionBlock(
        spec_id="38.211",
        release="Rel-19",
        clause=clause,
        section_title=title,
        section_level=level,
        body=body,
        body_chars=len(body),
        document_order=document_order,
    )


def test_stop_list_contents_dropped() -> None:
    sec = _sec(title="Contents", body="| Foreword | 8 |\n" * 30)
    is_drop, reason = is_garbage(sec)
    assert is_drop and reason and "stop-list" in reason


def test_stop_list_with_decorations_dropped() -> None:
    sec = _sec(title="**Copyright Notification**", body="x" * 200)
    assert is_garbage(sec)[0]


def test_preamble_dropped() -> None:
    sec = _sec(title="<preamble>", body="some intro text " * 20, clause="")
    assert is_garbage(sec)[0]


def test_clause_like_spec_id_dropped() -> None:
    sec = _sec(title="38.211 V19.0.0", body="some body text " * 30, clause="38.211")
    is_drop, reason = is_garbage(sec)
    assert is_drop and reason == "clause-looks-like-spec-id"


def test_clause_like_with_subpart_dropped() -> None:
    sec = _sec(title="38.101-1", body="x" * 200, clause="38.101-1")
    assert is_garbage(sec)[0]


def test_postal_address_dropped() -> None:
    body = (
        "3GPP support office address  \n650 Route des Lucioles - Sophia Antipolis  \n"
        "Valbonne - FRANCE"
    )
    sec = _sec(title="Postal address", body=body)
    assert is_garbage(sec)[0]


def test_short_postal_keyword_dropped() -> None:
    sec = _sec(title="3GPP", body="Sophia Antipolis France contact " * 5)
    is_drop, reason = is_garbage(sec)
    assert is_drop and reason == "contact-info"


def test_toc_table_dropped() -> None:
    body_lines = [f"| 5.{i} Item ..... | {i + 8} |" for i in range(40)]
    sec = _sec(title="Random Long Section", body="\n".join(body_lines))
    is_drop, reason = is_garbage(sec)
    assert is_drop and reason and reason.startswith("toc-table")


def test_table_heavy_technical_section_not_dropped() -> None:
    """复现 2026-05-28 报告的 DCI 1_1 误杀 bug。

    38.212 §7.3.1.2.2 (Format 1_1) 真实 body 含 12+ 张大型 DCI 字段查表，
    pipe_ratio ≈ 0.857 会命中旧版 TOC 启发式被静默丢弃，导致 BM25/向量索引完
    全缺失这一节，agent 检索 DCI 1_1 字段时返回不相关 IE hits。

    修复后 TOC 启发式要求 pipe 行多数 (> 0.5) 像 `| <页码> |` 结尾才判垃圾；
    技术表的右列是参数值 / bit pattern 不匹配，因此被正确保留。
    """
    # 真实采样：技术表行右列是 bit 模式 / 字段名 / 配置组合，不是页码
    rows = [
        "| 00     | First repetition factor                 |",
        "| 01     | Second repetition factor                |",
        "| 10     | Third repetition factor if provided     |",
        "| 11     | Fourth repetition factor if provided    |",
        "| Bit field | Antenna port(s) (1000 + DMRS port) |",
        "| 0      | port 1000                                |",
        "| 1      | ports 1000, 1001                         |",
        "| 2      | ports 1000, 1001, 1002, 1003            |",
        "|-----------|----------------------------------------|",
        "**Table 7.3.1.2.2-1: Antenna port(s) (1000 + DMRS port)**",
    ]
    body = "\n".join(rows * 6)  # 60 lines, >85% pipe-prefixed
    sec = _sec(title="Format 1\\_1", body=body, clause="7.3.1.2.2")
    is_drop, reason = is_garbage(sec)
    assert not is_drop, f"technical table section wrongly dropped: reason={reason!r}"


def test_real_toc_still_dropped_after_fix() -> None:
    """sanity check：修复后真 TOC（pipe 行末尾是页码）仍被正确丢弃。"""
    body_lines = [
        f"| {i//3 + 4}.{i%3 + 1} Some Section Title ..... | {i + 10} |" for i in range(60)
    ]
    sec = _sec(title="Random Wrapper", body="\n".join(body_lines), clause="x")
    is_drop, reason = is_garbage(sec)
    assert is_drop and reason and reason.startswith("toc-table") and "page-tails" in reason


def test_short_body_dropped() -> None:
    sec = _sec(title="Empty", body="x")
    assert is_garbage(sec)[0]


def test_normal_section_kept() -> None:
    body = (
        "This section describes the frame structure. "
        "Each frame consists of 10 subframes. "
        "Each subframe is 1 ms."
    ) * 10
    sec = _sec(title="Frame structure", body=body, clause="4.3")
    is_drop, _ = is_garbage(sec)
    assert not is_drop


def test_normal_table_section_kept() -> None:
    """少量表格（< 80% pipe lines）不应被误判为 TOC。"""
    body = (
        "Some prose introducing the table.\n"
        "Another paragraph with discussion.\n"
        "Third paragraph.\n"
        "| col | col |\n"
        "|-----|-----|\n"
        "| a   | b   |\n"
        "Continuation prose.\n"
    ) * 5
    sec = _sec(title="Symbols", body=body, clause="3.2")
    assert not is_garbage(sec)[0]


def test_filter_sections_keeps_order_and_collects_reasons() -> None:
    sections = [
        _sec(title="<preamble>", clause="", body="x" * 100, document_order=0),
        _sec(title="Foreword", clause="", body="x" * 200, document_order=1),
        _sec(title="Contents", clause="", body="| a | b |\n" * 30, document_order=2),
        _sec(title="1 Scope", clause="1", body="Scope text " * 30, document_order=3),
        _sec(title="4.3 Frame structure", clause="4.3", body="frame text " * 30, document_order=4),
    ]
    kept, dropped, reasons = filter_sections(sections)
    assert [k.section_title for k in kept] == ["1 Scope", "4.3 Frame structure"]
    assert {d.section_title for d in dropped} == {"<preamble>", "Foreword", "Contents"}
    # reasons 至少覆盖 3 个 dropped
    assert len(reasons) >= 3
