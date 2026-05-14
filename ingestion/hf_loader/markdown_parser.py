"""raw.md 章节解析。

3GPP markdown 通常这样：

```
# 3GPP TS 38.211 V19.0.0 (2025-09)
## 1 Scope
## 2 References
### 2.1 General
#### 5.3.1.1 Frame structure
```

H1 spec title 形如 `# 3GPP TS 38.211 V19.0.0 ...` 或 `# 3GPP TR 36.905 ...`。
我们把每个非 H1 标题视作 section，body 取到下一个 heading 之前。
clause 从标题开头的 "1.2.3" 数字提取；找不到的标题归为"非编号 section"，
后续 chunker 仍可处理，但 `clause` 留空（业务方知道这是 Annex / preface）。
"""

from __future__ import annotations

import re

from .models import SectionBlock

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
# clause 形如 "1.2.3" / "5" / "A.1.2"（附录），允许字母前缀。
_CLAUSE_RE = re.compile(r"^([A-Z]?[\dA-Z][\d.]*)\s+(.+)$", re.IGNORECASE)
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
# 标准 3GPP H1 形式：'# 3GPP TS 38.211 V19.0.0 (2025-09)'，也兼容 '# **3GPP TR ...'
_SPEC_TYPE_RE = re.compile(
    r"3GPP\s*\*{0,2}\s*(TS|TR)\s*\*{0,2}\s*(\d{2}\.\d{3}(?:-\d+)?)",
    re.IGNORECASE,
)


def detect_spec_type_and_title(text: str, *, spec_id: str | None = None) -> tuple[str, str | None]:
    """从 raw.md 头部 ~2KB 内识别 spec_type 与 title。

    返回 (spec_type, title)；spec_type ∈ {'TS','TR','unknown'}。

    优先级：
    1) 标准 H1 模板 '3GPP TS/TR XX.YYY' 直接采用
    2) 标题含 'Study on' / 'study item' / 'technical report' → TR
    3) 标题含 'Technical Specification' 或 'Technical Specification Group'
       且未触发 (2) 的 study → TS
    4) 若给了 spec_id，按 3GPP 编号约定（第二段 ≥ 500 → TR；< 500 → TS）兜底
    5) 仍无法判定 → unknown
    """
    head = text[:4096]
    h1_match = None
    for line in head.splitlines():
        line = line.strip()
        if line.startswith("# "):
            h1_match = line
            break
    candidate_pool = (h1_match or "") + "\n" + head

    # 1) 显式 '3GPP TS/TR XX.YYY'
    type_match = _SPEC_TYPE_RE.search(candidate_pool)
    if type_match:
        spec_type = type_match.group(1).upper()
        title = _clean_title(h1_match)
        return spec_type, title

    lower_pool = candidate_pool.lower()

    # 2) study 类 → TR
    if "study on" in lower_pool or "study item" in lower_pool or "technical report" in lower_pool:
        return "TR", _clean_title(h1_match)

    # 3) 标准 spec → TS（兜底）
    if "technical specification" in lower_pool:
        return "TS", _clean_title(h1_match)

    # 4) 按 spec_id 数字范围兜底
    if spec_id and "." in spec_id:
        try:
            second = spec_id.split(".", 1)[1].split("-", 1)[0]
            if second.isdigit():
                num = int(second)
                return ("TR" if num >= 500 else "TS"), _clean_title(h1_match)
        except (IndexError, ValueError):
            pass

    return "unknown", _clean_title(h1_match)


def _clean_title(h1_line: str | None) -> str | None:
    if h1_line is None:
        return None
    return h1_line.removeprefix("# ").strip().strip("* ").strip() or None


def parse_markdown_sections(text: str, *, spec_id: str, release: str) -> list[SectionBlock]:
    """把 raw.md 切成 SectionBlock 列表。

    实现要点：
    - raw.md 顶部的第一个 H1 通常是 spec 标题（形如 "38.211 V19.0.0 — ..."），
      它不是真正的章节，不进入 sections；其后的正文作为 preamble。
    - preamble（首个被认作章节的 heading 之前的所有正文）作为 document_order=0
      的 section，clause="" / title="<preamble>"。
    - body 取到下一个 heading 之前；不再做更细的层级 nest（父子关系由 chunker
      根据 clause 重建）。
    """
    matches = list(_HEADING_RE.finditer(text))

    # 顶部 H1（spec title）：第一个 heading 且 level==1 且整篇里 H1 仅它一个，
    # 就跳过，作为 spec title 看待。
    skip_first_h1 = False
    if matches and len(matches[0].group(1)) == 1:
        h1_count = sum(1 for m in matches if len(m.group(1)) == 1)
        if h1_count == 1:
            skip_first_h1 = True

    headings = matches[1:] if skip_first_h1 else matches
    sections: list[SectionBlock] = []

    preamble_start = matches[0].end() if skip_first_h1 else 0
    preamble_end = headings[0].start() if headings else len(text)
    preamble_body = text[preamble_start:preamble_end].strip()
    if preamble_body:
        sections.append(
            SectionBlock(
                spec_id=spec_id,
                release=release,
                clause="",
                section_title="<preamble>",
                section_level=0,
                body=preamble_body,
                body_chars=len(preamble_body),
                document_order=0,
                image_refs=tuple(_IMAGE_REF_RE.findall(preamble_body)),
            )
        )

    for idx, match in enumerate(headings):
        level = len(match.group(1))
        title_raw = match.group(2).strip()
        clause, section_title = _split_clause(title_raw)

        body_start = match.end()
        body_end = headings[idx + 1].start() if idx + 1 < len(headings) else len(text)
        body = text[body_start:body_end].strip()
        image_refs = tuple(_IMAGE_REF_RE.findall(body))

        sections.append(
            SectionBlock(
                spec_id=spec_id,
                release=release,
                clause=clause,
                section_title=section_title,
                section_level=level,
                body=body,
                body_chars=len(body),
                document_order=len(sections),
                image_refs=image_refs,
            )
        )

    return sections


def _split_clause(title_raw: str) -> tuple[str, str]:
    """把 '5.2.1 Frame structure' 拆成 ('5.2.1', 'Frame structure')。

    没有编号前缀（如 'Foreword' / 'Annex A'）时返回 ('', title_raw)。
    """
    match = _CLAUSE_RE.match(title_raw)
    if not match:
        return "", title_raw
    candidate = match.group(1)
    # 必须含至少一个数字，否则视作 'Annex' / 'Scope' 之类纯文本标题
    if not any(c.isdigit() for c in candidate):
        return "", title_raw
    return candidate, match.group(2).strip()


def extract_image_refs(text: str) -> list[str]:
    """从一段 markdown 中抽出所有图片引用（相对路径）。"""
    return _IMAGE_REF_RE.findall(text)
