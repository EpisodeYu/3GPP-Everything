"""Figure chunk 构造 + GSMA 自带描述抽取 + vision_resolver 接口（plan §4.5）。

GSMA raw.md 里图片下方常已带英文描述（看起来是 Marker pipeline 自带 vision），形如：

    ![Diagram of Non-Roaming 5G System Architecture](78d57...img.jpg)

    The diagram illustrates the Non-Roaming 5G System Architecture.
    At the top, a horizontal line represents the Service Based Interface ...

    Diagram of Non-Roaming 5G System Architecture
    Figure 4.1-1: Reference model – SMF.

抽取规则（来自 atomic_blocks._consume_figure 已经把图片 + 描述 + caption 整段
拉到一个 AtomicBlock(kind="figure")）：

- 第一行：`![alt](path)` → alt + path
- 紧跟（去空行）的若干段：GSMA 描述（直到 'Figure X.Y-N:' caption 行 或 文本结束）
- 最后一段（如有）：`Figure X.Y-N: ...` → spec_caption

vision_resolver 接口（plan §0 选定方案 Y）：

```
def vision_resolver(image_path: str, ctx: dict) -> dict | None:
    # ctx 包含 caption / surrounding_paragraph / spec_id / clause / gsma_alt /
    #         gsma_caption_text
    # 返回结构化 JSON：{"figure_kind", "visible_labels", "visible_acronyms",
    #                  "description", "spec_role"} 或 None（fallback 到 GSMA）
```

M1 阶段 vision_resolver=None 时，content 用 GSMA 自带描述拼装；vision.py 实现后
传入 resolver，content 用 JSON 的 `description` 字段，其余字段进 raw_extra。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .models import AtomicBlock

VisionResolver = Callable[[str, dict], dict | None]

_IMAGE_RE = re.compile(r"^\s*!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)\s*$")
_FIGURE_CAPTION_RE = re.compile(
    r"^\s*\**\s*(?P<cap>Figure\s+[A-Z]?\d+(?:\.\d+)*(?:-\d+)?\s*[:\.].*?)\**\s*$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class FigureExtract:
    """从 figure AtomicBlock 中抽取的结构化信息。

    所有字段都可能为 None / 空：figure_path 唯一保证非空。
    """

    image_alt: str
    image_path: str
    gsma_caption_text: str  # GSMA 自带的英文描述段（可能多段拼接）
    spec_caption: str | None  # 'Figure X.Y-N: ...' 标题


def extract_figure(block: AtomicBlock) -> FigureExtract | None:
    """从 figure AtomicBlock 中抽取 alt / path / GSMA 描述 / spec caption。"""
    if block.kind != "figure":
        return None

    lines = block.text.splitlines()
    if not lines:
        return None

    head = lines[0].strip()
    m = _IMAGE_RE.match(head)
    if not m:
        # block 不以 image 行起首：可能 atomic_blocks 误判，回退取 extra
        alt = block.extra.get("image_alt", "")
        path = block.extra.get("image_path", "")
    else:
        alt = m.group("alt").strip()
        path = m.group("path").strip()

    # 剩余行：跳过空行；找 spec caption（最后一行如匹配）
    body_lines = [ln for ln in lines[1:] if ln.strip()]

    spec_caption: str | None = None
    if body_lines:
        last = body_lines[-1].strip()
        cap_m = _FIGURE_CAPTION_RE.match(last)
        if cap_m:
            spec_caption = cap_m.group("cap").strip().rstrip(".")
            body_lines = body_lines[:-1]

    # 剩下的全部是 GSMA 描述段（可能多段，含 alt 复述）
    gsma_caption = "\n".join(body_lines).strip()
    # 去掉与 alt 完全相同的兜底重复行
    if gsma_caption and alt and gsma_caption.endswith(alt):
        gsma_caption = gsma_caption[: -len(alt)].rstrip()

    return FigureExtract(
        image_alt=alt,
        image_path=path,
        gsma_caption_text=gsma_caption,
        spec_caption=spec_caption,
    )


def build_figure_content(
    extract: FigureExtract,
    *,
    spec_id: str,
    clause: str,
    section_title: str,
    surrounding_paragraph: str | None = None,
    vision_resolver: VisionResolver | None = None,
) -> tuple[str, dict]:
    """构造 figure chunk 的 (content, raw_extra)。

    plan §4.5 的策略：
    - vision_resolver=None：用 GSMA 自带描述 + alt + spec_caption + surrounding 拼装
    - vision_resolver 返回 dict：用其 'description' 当主描述，其它字段进 raw_extra

    content 模板：
        [<spec_id> § <clause> <section_title>] Figure caption: <spec_caption>
        Description: <vision_desc or gsma_caption_text or alt>
        Visible labels: <comma-joined>            （仅 vision JSON 提供时）
        Visible acronyms: <comma-joined>          （仅 vision JSON 提供时）
        Context: <surrounding_paragraph>          （若提供）
    """
    raw_extra: dict = {
        "image_path": extract.image_path,
        "image_alt": extract.image_alt,
        "gsma_caption_text": extract.gsma_caption_text,
        "spec_caption": extract.spec_caption,
    }

    vision_data: dict | None = None
    if vision_resolver is not None:
        try:
            vision_data = vision_resolver(
                extract.image_path,
                {
                    "spec_id": spec_id,
                    "clause": clause,
                    "section_title": section_title,
                    "image_alt": extract.image_alt,
                    "spec_caption": extract.spec_caption,
                    "gsma_caption_text": extract.gsma_caption_text,
                    "surrounding_paragraph": surrounding_paragraph or "",
                },
            )
        except Exception as exc:  # pragma: no cover - 接口未来由 vision.py 兜底
            raw_extra["vision_error"] = f"{type(exc).__name__}: {exc}"
            vision_data = None

    # 选 description
    if vision_data:
        raw_extra["vision"] = vision_data
        description = (
            vision_data.get("description")
            or extract.gsma_caption_text
            or extract.image_alt
            or "(no description)"
        )
    else:
        description = extract.gsma_caption_text or extract.image_alt or "(no description)"

    header = f"[{spec_id} § {clause} {section_title}]".rstrip()
    parts: list[str] = [header]
    if extract.spec_caption:
        parts.append(f"Figure caption: {extract.spec_caption}")
    parts.append(f"Description: {description}")
    if vision_data:
        labels = vision_data.get("visible_labels") or []
        if labels:
            parts.append("Visible labels: " + ", ".join(map(str, labels)))
        acronyms = vision_data.get("visible_acronyms") or []
        if acronyms:
            parts.append("Visible acronyms: " + ", ".join(map(str, acronyms)))
    if surrounding_paragraph:
        parts.append(f"Context: {surrounding_paragraph.strip()}")

    return "\n".join(parts), raw_extra
