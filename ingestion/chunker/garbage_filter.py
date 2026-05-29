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
# 真 TOC 行的页码尾：`| 页码 |` / `| 页码 |` —— 末尾紧跟纯数字单元；技术表的
# 末列是参数值 / bit pattern / 配置组合，几乎不会匹配。两侧加 `|?` 兼容"末单元
# 后没有闭合管道"的渲染漂移。
_TOC_PAGE_TAIL_RE = re.compile(r"\|\s*\d{1,4}\s*\|?\s*$")
_SPEC_ID_LIKE_RE = re.compile(r"^\d{2}\.\d{3}(-\d+)?$")
# 标题包含 "3rd Generation Partnership Project" / "Technical Specification Group" 是
# 标准 spec 第二行 H2 模板段，content 多为 logo + 元信息，不是技术内容。
_SPEC_TEMPLATE_TITLE_RE = re.compile(
    r"3rd\s+Generation\s+Partnership\s+Project|Technical\s+Specification\s+Group",
    re.IGNORECASE,
)

# TOC 启发式的两条阈值：pipe 行占比 + pipe 行中"末尾是页码"的占比。
# 真 TOC 实测（38.212 Contents 段）：pipe_ratio=0.994，page_tail=0.959。
# 误判案例（38.212 §7.3.1.2.2 Format 1_1，含 12+ 张 DCI 字段查表）：
# pipe_ratio=0.857（超 0.80 阈值会触发 v1 误杀），page_tail=0.132。
# 加 page_tail > 0.5 作为 AND 条件可完美区分两类（详见 2026-05-28 DCI 1_1 handoff）。
_TOC_PIPE_RATIO_THRESHOLD = 0.80
_TOC_PAGE_TAIL_RATIO_THRESHOLD = 0.50


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

    # 规则 4：TOC 启发式（多数行以 | 起首 AND 多数 pipe 行像「目录页码行」）
    # 单条 pipe_ratio 阈值会误杀 DCI Format 1_1 这种含 12+ 张大型查表的真实
    # 技术段（实测 38.212 §7.3.1.2.2 pipe_ratio=0.857，被旧实现当 TOC 扔掉）。
    # 加 page_tail AND 条件后：真 TOC page_tail≈0.96 通过，技术段 ≈0.13 通过不了。
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) > 20:
        pipe_lines = [ln for ln in lines if _PIPE_LINE_RE.match(ln)]
        pipe_ratio = len(pipe_lines) / len(lines)
        if pipe_ratio > _TOC_PIPE_RATIO_THRESHOLD:
            page_tail = sum(1 for ln in pipe_lines if _TOC_PAGE_TAIL_RE.search(ln))
            page_tail_ratio = page_tail / max(len(pipe_lines), 1)
            if page_tail_ratio > _TOC_PAGE_TAIL_RATIO_THRESHOLD:
                return True, (
                    f"toc-table:{len(pipe_lines)}/{len(lines)}-pipe-lines"
                    f",{page_tail}-page-tails"
                )

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
