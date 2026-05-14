"""数据模型。

定义 GSMA HF 加载器的核心数据结构（不做任何 IO / 业务逻辑），
被 loader / spec_grouper / image_resolver / runner 共用。

字段口径来自 docs/03-development/02-ingestion-and-indexing.md §4.1。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SpecManifestEntry:
    """单篇 spec 在 GSMA marked/ 树下的元数据条目。

    `spec_uid` 是 GSMA 目录名（如 "38211" / "38101-1"），用于在 HF repo 内寻路；
    `spec_id`  是对外展示编号（如 "38.211" / "38.101-1"），由 spec_uid 派生。

    `source_doc_version` 来自 original/ 目录的 docx 文件名后缀，比如
    "38101-1-j50_cover.docx" → "j50"。同一 spec 的多个 docx 共享同一 version。
    """

    spec_uid: str
    spec_id: str
    spec_number: str
    spec_type: str  # "TS" / "TR" / "unknown"
    release: str  # "Rel-18" / "Rel-19"
    series: str  # "38"
    title: str | None
    raw_md_path: str  # marked/.../raw.md
    image_paths: tuple[str, ...] = field(default_factory=tuple)
    image_sizes: tuple[int, ...] = field(default_factory=tuple)
    raw_md_size: int = 0
    source_doc_path: str | None = None
    source_doc_version: str | None = None
    dataset_revision: str = ""

    @property
    def image_count(self) -> int:
        return len(self.image_paths)


@dataclass(slots=True)
class SectionBlock:
    """raw.md 解析得到的章节块。"""

    spec_id: str
    release: str
    clause: str  # "5.2.1"
    section_title: str
    section_level: int  # markdown 标题层数，#=1, ##=2 ...
    body: str  # 该章节内的 markdown 内容（不含标题）
    body_chars: int
    document_order: int
    image_refs: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class SpecBundle:
    """单篇 spec 完整加载结果。

    流式产出：每篇 spec 一个 bundle，外部 chunker 消费完一篇可立即释放。
    """

    entry: SpecManifestEntry
    sections: list[SectionBlock]
    raw_markdown: str
    dataset_revision: str

    @property
    def spec_id(self) -> str:
        return self.entry.spec_id

    @property
    def image_paths(self) -> Iterable[str]:
        return self.entry.image_paths
