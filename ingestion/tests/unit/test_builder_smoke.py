"""builder 端到端小烟雾测试：用合成的精简 SpecBundle 跑 build_chunks。"""

from __future__ import annotations

import textwrap

from ingestion.chunker import ChunkParams, build_chunks
from ingestion.chunker.tokenize_utils import count_tokens
from ingestion.hf_loader.models import SectionBlock, SpecBundle, SpecManifestEntry


def _bundle(sections: list[SectionBlock], spec_id: str = "38.211") -> SpecBundle:
    entry = SpecManifestEntry(
        spec_uid="38211",
        spec_id=spec_id,
        spec_number=spec_id,
        spec_type="TS",
        release="Rel-19",
        series="38",
        title="3GPP TS 38.211 V19.3.0",
        raw_md_path="marked/Rel-19/38_series/38211/raw.md",
        dataset_revision="testrev",
    )
    raw_md = "\n\n".join(s.body for s in sections)
    return SpecBundle(
        entry=entry,
        sections=sections,
        raw_markdown=raw_md,
        dataset_revision="testrev",
    )


def _sec(
    *,
    clause: str,
    title: str,
    body: str,
    level: int = 3,
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


def test_build_chunks_drops_garbage_keeps_real_sections() -> None:
    sections = [
        _sec(clause="", title="<preamble>", body="x" * 200, document_order=0),
        _sec(clause="", title="Foreword", body="x" * 200, document_order=1),
        _sec(clause="", title="Contents", body="| a | b |\n" * 30, document_order=2),
        _sec(
            clause="4.3",
            title="Frame structure",
            body="Each frame consists of 10 subframes. " * 30,
            document_order=3,
        ),
    ]
    chunks, stats = build_chunks(_bundle(sections))
    assert stats.sections_dropped >= 3
    assert stats.chunks_total > 0
    # 所有 chunk 都属于 4.3
    assert all(c.clause == "4.3" for c in chunks)


def test_build_chunks_injects_section_header() -> None:
    sec = _sec(
        clause="5.2.1",
        title="Pseudo-random sequence generation",
        body="Generic pseudo-random sequences are defined by a length-31 Gold sequence. " * 10,
        document_order=0,
    )
    chunks, _ = build_chunks(_bundle([sec]))
    assert chunks
    for c in chunks:
        assert c.content.startswith("[38.211 § 5.2.1 Pseudo-random sequence generation]")


def test_build_chunks_chunk_id_stable_across_runs() -> None:
    sec = _sec(
        clause="5.2.1",
        title="Test",
        body="Same body content " * 30,
        document_order=0,
    )
    c1, _ = build_chunks(_bundle([sec]))
    c2, _ = build_chunks(_bundle([sec]))
    assert [c.chunk_id for c in c1] == [c.chunk_id for c in c2]


def test_build_chunks_table_is_atomic_chunk_type() -> None:
    body = textwrap.dedent("""\
        Intro paragraph.

        Table 5.2.2-1: Definition.
        | col1 | col2 |
        |------|------|
        | a    | b    |
        | c    | d    |

        Trailing.
        """)
    sec = _sec(clause="5.2.2", title="Sequence", body=body)
    chunks, _ = build_chunks(_bundle([sec]))
    types = {c.chunk_type for c in chunks}
    assert "table" in types


def test_build_chunks_figure_chunk_uses_gsma_description() -> None:
    body = (
        "Some intro paragraph providing context.\n\n"
        "![A diagram of NR architecture](abc_img.jpg)\n\n"
        "The diagram illustrates the NR architecture with UE, AMF, SMF, UPF.\n"
        "The AMF connects to UE via N1.\n\n"
        "Figure 4.2-1: Non-Roaming 5G System Architecture.\n\n"
        "Following text."
    )
    sec = _sec(clause="4.2", title="Architecture", body=body)
    chunks, stats = build_chunks(_bundle([sec]))
    fig_chunks = [c for c in chunks if c.chunk_type == "figure"]
    assert len(fig_chunks) == 1
    fig = fig_chunks[0]
    assert "abc_img.jpg" in fig.raw_extra["image_path"]
    assert "diagram illustrates" in fig.content
    assert "Figure caption: Figure 4.2-1" in fig.content
    assert "Context: Some intro paragraph" in fig.content
    assert stats.figure_count == 1
    assert stats.figure_with_vision == 0


def test_build_chunks_no_chunk_too_oversize() -> None:
    """即使是长 paragraph，所有 chunk token 应在合理范围内（max × 1.5 内）。"""
    body = "This is a sentence that will be repeated many times. " * 500
    sec = _sec(clause="6.1", title="Big section", body=body)
    chunks, _ = build_chunks(
        _bundle([sec]),
        params=ChunkParams(target_tokens=200, max_tokens=300, overlap_tokens=30),
    )
    for c in chunks:
        assert count_tokens(c.content) <= 450, f"chunk too large: {count_tokens(c.content)} tokens"


def test_build_chunks_dedup_identical_content_within_section() -> None:
    """§6.1 of `2026-05-15-m1-poc-38331.md`：同 section 内若 splitter 输出两份完全相同的
    packed text，builder 应在 section 出口处 dedupe（保留首次出现）。"""
    # 同 section body 中重复出现等价的 action_list 短语 + 周边 paragraph，
    # 经 splitter packing 后可能产出 content 相同的副本 piece。
    body = (
        "Intro paragraph providing context for the section.\n\n"
        "- 1> for groupcast and broadcast; or\n\n"
        "- 1> for groupcast and broadcast; or\n\n"
        "- 1> for groupcast and broadcast; or\n"
    )
    sec = _sec(clause="5.8.3.2", title="Initiation", body=body)
    chunks, _ = build_chunks(_bundle([sec]))
    contents = [c.content for c in chunks]
    assert len(contents) == len(
        set(contents)
    ), f"duplicate chunk content found: {[c[:80] for c in contents]}"
    chunk_ids = [c.chunk_id for c in chunks]
    assert len(chunk_ids) == len(set(chunk_ids))


def test_build_chunks_dedup_cross_section_same_parent_id() -> None:
    """§6.1 of `2026-05-15-m1-poc-38331.md`：GSMA marker 把 `***field***` 等渲染成
    `####` 标题，clause 为空 + 同名 → 多 section 共享 parent_section_id；当各 section
    内描述完全一致时 chunk_id 也会撞。build_chunks 应在 spec 出口处做 chunk_id 去重。
    """
    shared_body = (
        "Duration of the measurement window in which to receive SS/PBCH blocks. "
        "It is given in number of subframes (see clause 5.5.2.10)."
    )
    sections = [
        _sec(clause="", title="***duration***", body=shared_body, document_order=0),
        _sec(clause="", title="***duration***", body=shared_body, document_order=1),
        _sec(clause="", title="***duration***", body=shared_body, document_order=2),
    ]
    chunks, _ = build_chunks(_bundle(sections))
    chunk_ids = [c.chunk_id for c in chunks]
    assert len(chunk_ids) == len(set(chunk_ids)), f"duplicate chunk_ids: {chunk_ids}"


def test_build_chunks_small_short_siblings_merged_into_one() -> None:
    """body 长度需 ≥ 30 chars 以通过 garbage_filter 的 empty-body 启发式。"""
    sections = [
        _sec(
            clause="5.1.1",
            title="BPSK",
            body="BPSK is a binary phase shift keying modulation scheme used widely.",
        ),
        _sec(
            clause="5.1.2",
            title="QPSK",
            body="QPSK uses four distinct phases to encode two bits per symbol.",
        ),
        _sec(
            clause="5.1.3",
            title="16QAM",
            body="16QAM combines amplitude and phase to encode four bits per symbol.",
        ),
    ]
    chunks, stats = build_chunks(_bundle(sections))
    assert stats.sections_merged >= 1
    # 合并后应仅产 1-2 个 chunk（不再每个 sibling 独占）
    assert stats.chunks_total <= 2
    assert any("BPSK" in c.content and "QPSK" in c.content for c in chunks)
