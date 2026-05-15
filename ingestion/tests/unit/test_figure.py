"""figure 单测：GSMA 描述抽取 / vision_resolver 调用 / fallback。"""

from __future__ import annotations

from ingestion.chunker.figure import (
    build_figure_content,
    extract_figure,
)
from ingestion.chunker.models import AtomicBlock


def _figure_block(text: str, *, image_alt: str = "", image_path: str = "x_img.jpg") -> AtomicBlock:
    return AtomicBlock(
        kind="figure",
        text=text,
        extra={"image_alt": image_alt, "image_path": image_path},
    )


def test_extract_figure_basic() -> None:
    text = (
        "![A diagram of NR architecture](abc_img.jpg)\n\n"
        "The diagram illustrates the NR architecture with UE, AMF, SMF, UPF.\n"
        "The AMF connects to UE via N1.\n\n"
        "Figure 4.2-1: Non-Roaming 5G System Architecture."
    )
    block = _figure_block(text, image_alt="A diagram of NR architecture", image_path="abc_img.jpg")
    extract = extract_figure(block)
    assert extract is not None
    assert extract.image_path == "abc_img.jpg"
    assert extract.image_alt == "A diagram of NR architecture"
    assert "diagram illustrates" in extract.gsma_caption_text
    assert extract.spec_caption is not None
    assert extract.spec_caption.startswith("Figure 4.2-1")


def test_extract_figure_no_caption() -> None:
    text = "![logo](logo_img.jpg)\n"
    block = _figure_block(text, image_alt="logo", image_path="logo_img.jpg")
    extract = extract_figure(block)
    assert extract is not None
    assert extract.spec_caption is None
    assert extract.gsma_caption_text == ""


def test_extract_figure_returns_none_for_non_figure() -> None:
    block = AtomicBlock(kind="paragraph", text="not a figure")
    assert extract_figure(block) is None


def test_build_figure_content_fallback_uses_gsma() -> None:
    text = (
        "![A diagram of NR architecture](abc_img.jpg)\n\n"
        "The diagram illustrates the NR architecture with UE, AMF, SMF, UPF."
    )
    block = _figure_block(text, image_alt="A diagram of NR architecture", image_path="abc_img.jpg")
    extract = extract_figure(block)
    assert extract is not None
    content, raw_extra = build_figure_content(
        extract,
        spec_id="23.501",
        clause="4.2",
        section_title="Architecture reference model",
        surrounding_paragraph="The 5G System architecture is service-based.",
        vision_resolver=None,
    )
    assert "[23.501 § 4.2 Architecture reference model]" in content
    assert "Description: The diagram illustrates the NR architecture" in content
    assert "Context: The 5G System architecture is service-based." in content
    assert raw_extra["image_path"] == "abc_img.jpg"
    assert raw_extra["gsma_caption_text"].startswith("The diagram illustrates")
    assert "vision" not in raw_extra


def test_build_figure_content_with_resolver_returns_structured() -> None:
    block = _figure_block(
        "![A diagram of NR architecture](abc_img.jpg)\n\nThe diagram illustrates ...",
        image_alt="A diagram of NR architecture",
        image_path="abc_img.jpg",
    )
    extract = extract_figure(block)
    assert extract is not None

    def resolver(image_path: str, ctx: dict) -> dict:
        return {
            "figure_kind": "architecture",
            "visible_labels": ["UE", "AMF", "SMF", "UPF"],
            "visible_acronyms": ["NR", "AMF", "SMF", "UPF"],
            "description": "The figure shows the 5G core network with UE, AMF, SMF, UPF.",
            "spec_role": "architecture diagram",
        }

    content, raw_extra = build_figure_content(
        extract,
        spec_id="23.501",
        clause="4.2",
        section_title="Architecture reference model",
        vision_resolver=resolver,
    )
    assert "Description: The figure shows the 5G core network" in content
    assert "Visible labels: UE, AMF, SMF, UPF" in content
    assert "Visible acronyms: NR, AMF, SMF, UPF" in content
    assert raw_extra["vision"]["figure_kind"] == "architecture"


def test_build_figure_content_with_failing_resolver_falls_back() -> None:
    block = _figure_block(
        "![A diagram](abc_img.jpg)\n\nDescription text from GSMA.",
        image_alt="A diagram",
        image_path="abc_img.jpg",
    )
    extract = extract_figure(block)
    assert extract is not None

    def bad_resolver(image_path: str, ctx: dict) -> dict | None:
        raise RuntimeError("vision pipeline down")

    content, raw_extra = build_figure_content(
        extract,
        spec_id="38.211",
        clause="6.3.3",
        section_title="Mapping to physical resources",
        vision_resolver=bad_resolver,
    )
    assert "Description: Description text from GSMA." in content
    assert "vision_error" in raw_extra
    assert raw_extra["vision_error"].startswith("RuntimeError")
