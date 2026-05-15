"""Section 级垃圾过滤。

GSMA raw.md 里每篇 spec 都带固定垃圾段：spec title 元信息（postal address）、
Contents（TOC 大表）、Foreword、Copyright Notification 等；它们对检索毫无价值，
进 embedding 还会污染向量空间。

两级过滤：
1. **stop-list 精确匹配** section_title（不区分大小写、去 markdown 装饰符后比对）
2. **启发式兜底**：
   - body 中 `|` 起首的行占比 > 80% 且行数 > 20 → 视为 TOC
   - body 含 "Postal address" / "Sophia Antipolis" / "Internet" 任一关键词且 < 600 字符
   - body 去掉空白后 < 30 字符 → 孤儿空段
   - clause 长得像 spec_id（"38.211" / "38.331-1"）→ 见 hf-loader 注意点 6.3

返回 (kept, dropped, reason_map)，reason_map 用于 debug / audit；
kept 维持原 document_order。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ingestion.hf_loader.models import SectionBlock

# stop-list（小写、去掉 markdown 装饰符）
_STOP_TITLES: frozenset[str] = frozenset(
    {
        "contents",
        "foreword",
        "copyright notification",
        "<preamble>",
        "introduction",  # 多数 spec 是空 / 无技术内容；保留 1 Scope 起的内容
        "postal address",
        "keywords",
        "trademarks",
        "intellectual property rights",
    }
)

_DECOR_RE = re.compile(r"[*_`#\s]+")
_PIPE_LINE_RE = re.compile(r"^\s*\|")
_SPEC_ID_LIKE_RE = re.compile(r"^\d{2}\.\d{3}(-\d+)?$")
# 标题包含 "3rd Generation Partnership Project" / "Technical Specification Group" 是
# 标准 spec 第二行 H2 模板段，content 多为 logo + 元信息，不是技术内容。
_SPEC_TEMPLATE_TITLE_RE = re.compile(
    r"3rd\s+Generation\s+Partnership\s+Project|Technical\s+Specification\s+Group",
    re.IGNORECASE,
)


def _normalize_title(title: str) -> str:
    """去掉 markdown 装饰符与首尾空白后转小写，用于 stop-list 比对。"""
    cleaned = _DECOR_RE.sub(" ", title).strip().lower()
    return " ".join(cleaned.split())


def is_garbage(section: SectionBlock) -> tuple[bool, str | None]:
    """判定单个 section 是否垃圾；返回 (is_garbage, reason)。"""
    norm_title = _normalize_title(section.section_title)

    # 规则 0：clause 像 spec_id（hf-loader 注意点 6.3 的 spec title 伪 section）
    if section.clause and _SPEC_ID_LIKE_RE.match(section.clause):
        return True, "clause-looks-like-spec-id"

    # 规则 1：stop-list
    if norm_title in _STOP_TITLES:
        return True, f"stop-list:{norm_title}"

    # 规则 1.5：spec 模板 H2 段（"3rd Generation Partnership Project ..."）
    if _SPEC_TEMPLATE_TITLE_RE.search(section.section_title):
        return True, "spec-template-title"

    body = section.body or ""
    body_stripped = body.strip()

    # 规则 2：空段
    if len(body_stripped) < 30:
        return True, f"empty-body:{len(body_stripped)}chars"

    # 规则 3：postal/contact 元信息（小段且含关键词）
    contact_kw = ("Postal address", "Sophia Antipolis", "Internet", "3GPP support office")
    if len(body_stripped) < 600 and any(kw in body for kw in contact_kw):
        return True, "contact-info"

    # 规则 4：TOC 启发式（多数行以 | 起首）
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) > 20:
        pipe_lines = sum(1 for ln in lines if _PIPE_LINE_RE.match(ln))
        if pipe_lines / len(lines) > 0.8:
            return True, f"toc-table:{pipe_lines}/{len(lines)}-pipe-lines"

    return False, None


def filter_sections(
    sections: Iterable[SectionBlock],
) -> tuple[list[SectionBlock], list[SectionBlock], dict[int, str]]:
    """对 SectionBlock 列表跑垃圾过滤。

    返回 (kept, dropped, reason_map)：
    - `kept` / `dropped` 维持原顺序
    - `reason_map[document_order]` = 丢弃原因（仅 dropped 中的 section 出现）
    """
    kept: list[SectionBlock] = []
    dropped: list[SectionBlock] = []
    reasons: dict[int, str] = {}

    for sec in sections:
        is_drop, reason = is_garbage(sec)
        if is_drop:
            dropped.append(sec)
            if reason is not None:
                reasons[sec.document_order] = reason
        else:
            kept.append(sec)

    return kept, dropped, reasons
