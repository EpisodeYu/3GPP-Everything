"""短 sibling section 合并（plan §4.4）。

GSMA spec 里大量孤儿小节（如 38.211 的 5.1.1 BPSK / 5.1.2 QPSK / 5.1.3 16QAM）
本身只有几十 token；如果直接进 splitter 会产出大量"标题占满"的 chunk。

合并规则：
- 同 parent clause（5.1.* 共享 parent="5.1"）下连续的 section
- 任一 section body < 200 tokens（target 的 80%）
- 合并到一个虚拟 section：clause 用 parent（"5.1"）、title 用占位
  "<merged: 5.1.1 / 5.1.2 / 5.1.3>"；body 拼成

      ### 5.1.1 BPSK
      <body>

      ### 5.1.2 QPSK
      <body>

  保留各自的子标题作为 inline heading
- 合并后总 tokens 仍 < max_tokens 的话，splitter 会产 1 个 chunk；超过则照常切

边界：
- `<preamble>` / clause 为空的非编号 section 不参与合并
- spec 的最顶层 clause（如 "1" Scope / "2" References）单独成段不合并
"""

from __future__ import annotations

import logging

from ingestion.hf_loader.models import SectionBlock

from .tokenize_utils import count_tokens

log = logging.getLogger(__name__)


def parent_clause(clause: str) -> str | None:
    """'5.1.2' → '5.1'；'5.1' → '5'；'5' → None；'' → None。

    附录形式 'A.1.2' → 'A.1'；'A.1' → 'A'；'A' → None。
    """
    if not clause:
        return None
    if "." not in clause:
        return None
    return clause.rsplit(".", 1)[0]


def merge_short_siblings(
    sections: list[SectionBlock],
    *,
    short_threshold_tokens: int = 200,
    target_tokens: int = 250,
    max_tokens: int = 400,
) -> list[SectionBlock]:
    """合并相邻短 sibling section。

    返回新 list[SectionBlock]：
    - 不参与合并的原样保留（含 clause / level / image_refs）
    - 合并组用一个新的 SectionBlock 替代，clause = parent_clause，section_title 用
      占位，body 为子标题 + 子 body 的拼接，image_refs 是各子 image_refs 之和
    """
    out: list[SectionBlock] = []
    i = 0
    n = len(sections)

    while i < n:
        sec = sections[i]
        if not _is_mergeable(sec, short_threshold_tokens):
            out.append(sec)
            i += 1
            continue

        parent = parent_clause(sec.clause)
        if parent is None:
            out.append(sec)
            i += 1
            continue

        # 收集同 parent 下连续的 mergeable sibling
        group: list[SectionBlock] = [sec]
        running_tokens = count_tokens(sec.body)
        j = i + 1
        while j < n:
            nxt = sections[j]
            if _is_mergeable(nxt, short_threshold_tokens) and parent_clause(nxt.clause) == parent:
                add_tokens = count_tokens(nxt.body)
                # 加入后超过 max_tokens 就先封口
                if running_tokens + add_tokens > max_tokens:
                    break
                group.append(nxt)
                running_tokens += add_tokens
                j += 1
                continue
            break

        if len(group) <= 1:
            out.append(sec)
            i += 1
            continue

        # 合并：> 1 个 sibling 时才真合
        merged = _merge_group(group, parent_clause_value=parent)
        out.append(merged)
        i = j

    return out


def _is_mergeable(sec: SectionBlock, threshold: int) -> bool:
    """section 是否符合合并候选。"""
    if not sec.clause:
        return False  # preamble / 非编号 section 不合并
    if sec.section_title.startswith("<"):
        return False  # 已是占位 / 已合并
    if "." not in sec.clause:
        return False  # 顶层 clause 不参与（"1 Scope" 等）
    body_tokens = count_tokens(sec.body)
    return body_tokens < threshold


def _merge_group(group: list[SectionBlock], *, parent_clause_value: str) -> SectionBlock:
    """把 group 内的 SectionBlock 合并成一个虚拟 section。

    新 section：
    - clause = parent_clause_value
    - section_title = '<merged: 5.1.1 / 5.1.2 / 5.1.3>'
    - section_level = group[0].section_level - 1（取负数则保持 group[0] 的 level）
    - body = '### 5.1.1 BPSK\n<body>\n\n### 5.1.2 QPSK\n<body>\n...'
    - image_refs = 各 image_refs 平铺
    - document_order = group[0].document_order
    """
    parts: list[str] = []
    image_refs: list[str] = []
    for child in group:
        heading_marker = "#" * max(1, child.section_level)
        parts.append(f"{heading_marker} {child.clause} {child.section_title}".rstrip())
        if child.body.strip():
            parts.append(child.body.rstrip())
        image_refs.extend(child.image_refs)

    merged_body = "\n\n".join(parts)
    title = f"<merged: {' / '.join(c.clause for c in group)}>"
    return SectionBlock(
        spec_id=group[0].spec_id,
        release=group[0].release,
        clause=parent_clause_value,
        section_title=title,
        section_level=max(1, group[0].section_level - 1),
        body=merged_body,
        body_chars=len(merged_body),
        document_order=group[0].document_order,
        image_refs=tuple(image_refs),
    )
