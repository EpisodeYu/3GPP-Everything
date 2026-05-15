"""Chunker 数据契约。

只放数据结构，不做任何 IO/业务。被 atomic_blocks / section_splitter / merger /
figure / builder 共享。

字段口径来自 docs/03-development/02-ingestion-and-indexing.md §4.3 +
本期 small2big 决策（plan §3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ChunkType = Literal["text", "table", "formula", "figure", "asn1", "action_list", "section_head"]
AtomicKind = Literal[
    "paragraph", "table", "formula_block", "figure", "asn1", "action_list", "blank"
]


@dataclass(slots=True)
class AtomicBlock:
    """raw markdown section body 经"原子化"后的最小逻辑块。

    每个块要么整块进 chunk，要么有自己的"原子内切片"策略（见 atomic_blocks.py 注释）。
    `text` 是该块在原 section body 中对应的子串（含原始 markdown）。
    """

    kind: AtomicKind
    text: str
    extra: dict = field(default_factory=dict)

    @property
    def is_atomic(self) -> bool:
        """除 paragraph 外都视为原子（不可任意切）。"""
        return self.kind != "paragraph"


@dataclass(slots=True)
class Chunk:
    """最终入 embedding/Qdrant 的 chunk。

    `chunk_id` = uuid5(spec_id + clause + sha256(content)[:16])，跨 dataset_revision
    内容不变 → 同 ID，重跑真正幂等（见 plan §3）。

    `parent_section_id` = uuid5(spec_id + clause)，是 small2big 召回的 group key：
    召回时按此 key 取整段 section 给 LLM；当 parent_section_chars 过大时由召回层
    退化为相邻 N chunk 拼接。
    """

    chunk_id: str
    spec_id: str
    spec_uid: str | None
    spec_number: str
    spec_type: str
    release: str
    series: str
    title: str
    chunk_type: ChunkType
    clause: str
    section_path: tuple[str, ...]
    section_title: str
    parent_section_id: str
    parent_section_chars: int
    document_order: int
    content: str
    raw_extra: dict
    cross_refs: list[str]
    source: Literal["gsma_hf", "docling_fallback"]
    source_version: str
    created_at: datetime

    @property
    def content_chars(self) -> int:
        return len(self.content)
