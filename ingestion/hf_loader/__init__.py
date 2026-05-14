"""GSMA/3GPP HuggingFace dataset 加载器（主路径）。

公共入口：

- `GsmaHfLoader`            HF 树枚举 + 流式 SpecBundle 产出
- `SpecManifestEntry`       manifest 行（spec 元数据）
- `SpecBundle`              单 spec 完整加载结果（含 sections + raw markdown）
- `SectionBlock`            raw.md 解析后的章节块
- `manifest_session`        SQLite manifest 上下文管理器
- `parse_markdown_sections` 独立暴露的 markdown 解析函数
- `resolve_image`           HF 图片下载 + sha256

详见 docs/03-development/02-ingestion-and-indexing.md §4.1。
"""

from .image_resolver import ResolvedImage, hash_bytes, resolve_image
from .loader import (
    DEFAULT_RELEASES,
    GSMA_REPO_ID,
    GsmaHfLoader,
    LoaderStats,
)
from .manifest_store import (
    get_meta,
    manifest_session,
    open_manifest,
    read_entries,
    set_meta,
    write_entries,
)
from .markdown_parser import (
    detect_spec_type_and_title,
    extract_image_refs,
    parse_markdown_sections,
)
from .models import SectionBlock, SpecBundle, SpecManifestEntry
from .spec_grouper import (
    TS_5G_SERIES_WHITELIST,
    dedupe_keep_latest,
    filter_ts_5g,
    parse_doc_version,
    parse_spec_uid,
    release_rank,
)

__all__ = [
    "DEFAULT_RELEASES",
    "GSMA_REPO_ID",
    "TS_5G_SERIES_WHITELIST",
    "GsmaHfLoader",
    "LoaderStats",
    "ResolvedImage",
    "SectionBlock",
    "SpecBundle",
    "SpecManifestEntry",
    "dedupe_keep_latest",
    "detect_spec_type_and_title",
    "extract_image_refs",
    "filter_ts_5g",
    "get_meta",
    "hash_bytes",
    "manifest_session",
    "open_manifest",
    "parse_doc_version",
    "parse_markdown_sections",
    "parse_spec_uid",
    "read_entries",
    "release_rank",
    "resolve_image",
    "set_meta",
    "write_entries",
]
