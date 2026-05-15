"""garbage_filter 补测：spec 模板 H2 段过滤。"""

from __future__ import annotations

from ingestion.chunker.garbage_filter import is_garbage
from ingestion.hf_loader.models import SectionBlock


def _sec(*, title: str, body: str = "x" * 200, clause: str = "") -> SectionBlock:
    return SectionBlock(
        spec_id="38.211",
        release="Rel-19",
        clause=clause,
        section_title=title,
        section_level=2,
        body=body,
        body_chars=len(body),
        document_order=1,
    )


def test_spec_template_h2_dropped() -> None:
    sec = _sec(
        title=(
            "**3rd Generation Partnership Project; Technical Specification Group "
            "Radio Access Network; NR; Physical channels and modulation (Release 19)**"
        ),
        body="![5G logo](abc_img.jpg)\n\nThe 5G Advanced logo." * 20,
    )
    is_drop, reason = is_garbage(sec)
    assert is_drop and reason == "spec-template-title"


def test_technical_specification_group_only_dropped() -> None:
    sec = _sec(title="Technical Specification Group Services", body="x" * 200)
    assert is_garbage(sec)[0]


def test_normal_h2_with_keyword_kept() -> None:
    sec = _sec(
        title="4.3 Frame structure",
        body="The frame structure consists of 10 subframes." * 5,
        clause="4.3",
    )
    assert not is_garbage(sec)[0]
