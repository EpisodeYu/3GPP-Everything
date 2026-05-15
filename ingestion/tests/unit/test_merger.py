"""merger 单测：parent_clause 计算 / 短 sibling 合并 / 边界。"""

from __future__ import annotations

from ingestion.chunker.merger import merge_short_siblings, parent_clause
from ingestion.hf_loader.models import SectionBlock


def _sec(
    *,
    clause: str,
    title: str,
    body: str = "x" * 50,
    level: int = 4,
    document_order: int = 0,
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


def test_parent_clause_basic() -> None:
    assert parent_clause("5.1.2") == "5.1"
    assert parent_clause("5.1") == "5"
    assert parent_clause("5") is None
    assert parent_clause("") is None
    assert parent_clause("A.1.2") == "A.1"


def test_three_short_siblings_merged() -> None:
    secs = [
        _sec(clause="5.1.1", title="BPSK", body="short body BPSK", document_order=0),
        _sec(clause="5.1.2", title="QPSK", body="short body QPSK", document_order=1),
        _sec(clause="5.1.3", title="16QAM", body="short body 16QAM", document_order=2),
    ]
    merged = merge_short_siblings(secs, short_threshold_tokens=200)
    assert len(merged) == 1
    out = merged[0]
    assert out.clause == "5.1"
    assert "merged" in out.section_title
    assert "BPSK" in out.body
    assert "QPSK" in out.body
    assert "16QAM" in out.body


def test_long_sibling_breaks_merge() -> None:
    long_body = "long body content " * 100  # ~ 200+ tokens
    secs = [
        _sec(clause="5.1.1", title="BPSK", body="short A", document_order=0),
        _sec(clause="5.1.2", title="QPSK", body=long_body, document_order=1),
        _sec(clause="5.1.3", title="16QAM", body="short C", document_order=2),
    ]
    merged = merge_short_siblings(secs, short_threshold_tokens=20)
    # 第一个孤儿不与长 sibling 合（因长 sibling 不参与），所以 5.1.1 单独保留；
    # 5.1.2 单独保留；5.1.3 单独保留（前后无 mergeable 邻居）。
    assert len(merged) == 3


def test_top_level_clause_not_merged() -> None:
    secs = [
        _sec(clause="1", title="Scope", body="short", level=2, document_order=0),
        _sec(clause="2", title="References", body="short", level=2, document_order=1),
    ]
    merged = merge_short_siblings(secs)
    assert len(merged) == 2
    assert merged[0].clause == "1"
    assert merged[1].clause == "2"


def test_preamble_not_merged() -> None:
    secs = [
        _sec(clause="", title="<preamble>", body="short", document_order=0),
        _sec(clause="5.1.1", title="A", body="short", document_order=1),
        _sec(clause="5.1.2", title="B", body="short", document_order=2),
    ]
    merged = merge_short_siblings(secs)
    assert merged[0].clause == ""
    assert merged[0].section_title == "<preamble>"
    # 5.1.1 + 5.1.2 应被合并
    assert any("merged" in s.section_title for s in merged[1:])


def test_different_parents_not_merged() -> None:
    secs = [
        _sec(clause="5.1.1", title="A", body="short", document_order=0),
        _sec(clause="5.2.1", title="B", body="short", document_order=1),
        _sec(clause="5.3.1", title="C", body="short", document_order=2),
    ]
    merged = merge_short_siblings(secs)
    # 三个 parent 各 1 个 sibling，无法合并
    assert len(merged) == 3


def test_merge_image_refs_preserved() -> None:
    secs = [
        SectionBlock(
            spec_id="38.211",
            release="Rel-19",
            clause="5.1.1",
            section_title="A",
            section_level=4,
            body="short",
            body_chars=5,
            document_order=0,
            image_refs=("a.jpg",),
        ),
        SectionBlock(
            spec_id="38.211",
            release="Rel-19",
            clause="5.1.2",
            section_title="B",
            section_level=4,
            body="short",
            body_chars=5,
            document_order=1,
            image_refs=("b.jpg",),
        ),
    ]
    merged = merge_short_siblings(secs)
    assert len(merged) == 1
    assert set(merged[0].image_refs) == {"a.jpg", "b.jpg"}
