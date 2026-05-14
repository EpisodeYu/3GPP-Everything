"""GSMA/3GPP HF dataset 加载器（主路径）。

只做两件事：
1. 枚举 `marked/Rel-{N}/{NN}_series/{spec_uid}/` 文件树，组装 `SpecManifestEntry` 列表。
2. 按 entry 拉 raw.md，解析章节，流式产出 `SpecBundle`。

不做什么（避免越权）：
- 不实例化 chunker / embedder / Qdrant client（那些归 chunker / indexer 模块）。
- 不调 Vision（归 ingestion/images/）。
- 不直接读 Postgres / Redis（manifest 持久化在 §4.1 是 SQLite/parquet，由 runner 决定）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.hf_api import RepoFile, RepoFolder

from .markdown_parser import parse_markdown_sections
from .models import SpecBundle, SpecManifestEntry
from .spec_grouper import (
    TS_5G_SERIES_WHITELIST,
    dedupe_keep_latest,
    filter_ts_5g,
    parse_doc_version,
    parse_spec_uid,
)

GSMA_REPO_ID = "GSMA/3GPP"
DEFAULT_RELEASES: tuple[str, ...] = ("Rel-18", "Rel-19")
_RAW_MD_FILENAME = "raw.md"

log = logging.getLogger(__name__)


@dataclass(slots=True)
class LoaderStats:
    """单次扫描统计，供 audit/runner 输出报告使用。"""

    releases_scanned: list[str] = field(default_factory=list)
    series_scanned: list[str] = field(default_factory=list)
    raw_entries: int = 0
    raw_md_total_bytes: int = 0
    image_files_total: int = 0
    series_distribution: dict[str, int] = field(default_factory=dict)
    release_distribution: dict[str, int] = field(default_factory=dict)
    ts_count: int = 0
    tr_count: int = 0
    unknown_type_count: int = 0


class GsmaHfLoader:
    """GSMA HF 数据集加载器。

    每个实例绑定一次 `dataset_revision`：
    - 若构造时没传 revision，用 HfApi 拉一次 dataset_info 自动 pin
    - 之后所有 list_repo_tree / hf_hub_download 都带这个 revision
    这样 manifest / chunk / payload / PG metadata 都能追溯到同一份快照。
    """

    def __init__(
        self,
        *,
        revision: str | None = None,
        token: str | None = None,
        cache_dir: str | Path | None = None,
        endpoint: str | None = None,
        api: HfApi | None = None,
    ) -> None:
        self._api = api or HfApi(endpoint=endpoint, token=token)
        self._token = token
        self._cache_dir = str(cache_dir) if cache_dir else None
        if revision:
            self.revision = revision
        else:
            info = self._api.dataset_info(GSMA_REPO_ID)
            self.revision = info.sha

    # ---- 枚举阶段 ----

    def list_release_root(self, release: str) -> list[RepoFolder]:
        """返回 `marked/<release>/` 下的 `<NN>_series` 目录列表。"""
        entries = self._api.list_repo_tree(
            repo_id=GSMA_REPO_ID,
            repo_type="dataset",
            revision=self.revision,
            path_in_repo=f"marked/{release}",
            recursive=False,
        )
        return [e for e in entries if isinstance(e, RepoFolder)]

    def list_series(self, release: str, series_folder: str) -> list[RepoFolder]:
        """返回 `marked/<release>/<NN>_series/` 下的 spec_uid 目录列表。"""
        entries = self._api.list_repo_tree(
            repo_id=GSMA_REPO_ID,
            repo_type="dataset",
            revision=self.revision,
            path_in_repo=f"marked/{release}/{series_folder}",
            recursive=False,
        )
        return [e for e in entries if isinstance(e, RepoFolder)]

    def list_spec_dir(self, release: str, series_folder: str, spec_uid: str) -> list[RepoFile]:
        """返回 spec 目录内的所有文件（raw.md + 图片）。"""
        entries = self._api.list_repo_tree(
            repo_id=GSMA_REPO_ID,
            repo_type="dataset",
            revision=self.revision,
            path_in_repo=f"marked/{release}/{series_folder}/{spec_uid}",
            recursive=False,
        )
        return [e for e in entries if isinstance(e, RepoFile)]

    def list_original_series(self, release: str, series_folder: str) -> list[RepoFile]:
        """返回 `original/<release>/<NN>_series/` 下的所有 doc/docx/zip 文件。

        GSMA original 目录是平铺的（不按 spec_uid 分子目录），多份 docx 共享同一
        spec_uid + version；本模块只取第一份用于 source_doc_path / version 记录。
        """
        try:
            entries = self._api.list_repo_tree(
                repo_id=GSMA_REPO_ID,
                repo_type="dataset",
                revision=self.revision,
                path_in_repo=f"original/{release}/{series_folder}",
                recursive=False,
            )
        except Exception as exc:  # pragma: no cover - 网络/权限失败时不致命
            log.warning("list_original_series failed for %s/%s: %s", release, series_folder, exc)
            return []
        return [e for e in entries if isinstance(e, RepoFile)]

    # ---- manifest 构建 ----

    def build_manifest(
        self,
        *,
        releases: Iterable[str] = DEFAULT_RELEASES,
        include_original: bool = True,
        progress: bool = False,
    ) -> tuple[list[SpecManifestEntry], LoaderStats]:
        """扫描指定 releases，返回 (entries, stats)。

        - 不做去重、不做白名单过滤；调用方按需用 spec_grouper 处理。
        - 全程只读 HF tree API，不下载任何 raw.md / 图片。
        - 实现优化：每个 series 用 `recursive=True` 一次拉完该 series 下所有
          spec_uid 子目录的 raw.md + 图片，避免 N 次单 spec 调用。
          扫单个 release 实测约 30-60s（含 original/）。
        """
        entries: list[SpecManifestEntry] = []
        stats = LoaderStats(releases_scanned=list(releases))

        for release in releases:
            release_series_folders = self.list_release_root(release)
            series_names = sorted({_strip_series_suffix(f.path) for f in release_series_folders})
            stats.series_scanned = sorted(set(stats.series_scanned + series_names))

            for folder in release_series_folders:
                series_folder = _basename(folder.path)
                series_name = _strip_series_suffix(series_folder)

                original_version_map = self._collect_original_version_map(
                    release, series_folder, include_original
                )
                series_entries = self._enumerate_series_specs(
                    release, series_folder, series_name, original_version_map
                )

                for entry in series_entries:
                    entries.append(entry)
                    stats.raw_entries += 1
                    stats.raw_md_total_bytes += entry.raw_md_size
                    stats.image_files_total += entry.image_count
                    stats.series_distribution[entry.series] = (
                        stats.series_distribution.get(entry.series, 0) + 1
                    )
                    stats.release_distribution[release] = (
                        stats.release_distribution.get(release, 0) + 1
                    )
                    if entry.spec_type == "TS":
                        stats.ts_count += 1
                    elif entry.spec_type == "TR":
                        stats.tr_count += 1
                    else:
                        stats.unknown_type_count += 1

                if progress:
                    log.info(
                        "scanned %s/%s: %d entries so far",
                        release,
                        series_folder,
                        stats.raw_entries,
                    )

        return entries, stats

    def _enumerate_series_specs(
        self,
        release: str,
        series_folder: str,
        series_name: str,
        original_version_map: dict[str, tuple[str, str | None]],
    ) -> list[SpecManifestEntry]:
        """一次 recursive 拉单个 series 下的所有 spec 文件。

        把按 spec_uid 分组的文件聚合成 SpecManifestEntry。
        """
        try:
            tree_entries = list(
                self._api.list_repo_tree(
                    repo_id=GSMA_REPO_ID,
                    repo_type="dataset",
                    revision=self.revision,
                    path_in_repo=f"marked/{release}/{series_folder}",
                    recursive=True,
                )
            )
        except Exception as exc:  # pragma: no cover
            log.warning("recursive tree failed for %s/%s: %s", release, series_folder, exc)
            return []

        # 按 spec_uid 分组（spec_uid = 'marked/Rel-19/38_series/38211/...' 的倒数第二段）
        spec_files: dict[str, list[RepoFile]] = {}
        prefix = f"marked/{release}/{series_folder}/"
        for entry in tree_entries:
            if not isinstance(entry, RepoFile):
                continue
            if not entry.path.startswith(prefix):
                continue
            rest = entry.path[len(prefix) :]
            spec_uid, _, _ = rest.partition("/")
            if not spec_uid:
                continue
            spec_files.setdefault(spec_uid, []).append(entry)

        out: list[SpecManifestEntry] = []
        for spec_uid in sorted(spec_files):
            files = spec_files[spec_uid]
            raw_md = next((f for f in files if _basename(f.path) == _RAW_MD_FILENAME), None)
            if raw_md is None:
                log.debug("skip %s/%s/%s: no raw.md", release, series_folder, spec_uid)
                continue
            images = sorted(
                (f for f in files if _basename(f.path).endswith("_img.jpg")),
                key=lambda f: f.path,
            )
            source_doc_path, source_doc_version = original_version_map.get(spec_uid, (None, None))
            series_parsed, spec_id, _ = parse_spec_uid(spec_uid)
            series_value = series_parsed or series_name
            spec_type = _infer_spec_type(series_value)
            out.append(
                SpecManifestEntry(
                    spec_uid=spec_uid,
                    spec_id=spec_id,
                    spec_number=spec_id,
                    spec_type=spec_type,
                    release=release,
                    series=series_value,
                    title=None,
                    raw_md_path=raw_md.path,
                    image_paths=tuple(f.path for f in images),
                    image_sizes=tuple(f.size or 0 for f in images),
                    raw_md_size=raw_md.size or 0,
                    source_doc_path=source_doc_path,
                    source_doc_version=source_doc_version,
                    dataset_revision=self.revision,
                )
            )
        return out

    def _collect_original_version_map(
        self, release: str, series_folder: str, include_original: bool
    ) -> dict[str, tuple[str, str | None]]:
        """从 `original/<release>/<series>/` 平铺文件名中提取 spec_uid → (path, version)。"""
        out: dict[str, tuple[str, str | None]] = {}
        if not include_original:
            return out
        for doc in self.list_original_series(release, series_folder):
            spec_uid, version = _parse_original_filename(_basename(doc.path))
            if not spec_uid:
                continue
            prev = out.get(spec_uid)
            if prev is None or (version and (prev[1] is None or version > prev[1])):
                out[spec_uid] = (doc.path, version)
        return out

    # ---- 应用 §2 过滤策略 ----

    @staticmethod
    def apply_production_filter(
        entries: list[SpecManifestEntry],
        *,
        whitelist: frozenset[str] = TS_5G_SERIES_WHITELIST,
    ) -> list[SpecManifestEntry]:
        """生产口径：跨 release 同 spec_id 保留最新 + TS 5G 系列白名单。"""
        deduped = dedupe_keep_latest(entries)
        return filter_ts_5g(deduped, whitelist=whitelist)

    # ---- 加载阶段：流式 ----

    def iter_specs(
        self, entries: Iterable[SpecManifestEntry], *, parse_sections: bool = True
    ) -> Iterator[SpecBundle]:
        """逐篇下载 raw.md → 解析章节 → 产出 SpecBundle。

        每次 yield 后立即放手对 raw markdown 的引用，外部消费方应及时持久化 chunk
        以避免内存爆掉（生产 ~1296 篇时 raw.md 总和约 621MiB）。
        """
        for entry in entries:
            local = hf_hub_download(
                repo_id=GSMA_REPO_ID,
                filename=entry.raw_md_path,
                repo_type="dataset",
                revision=self.revision,
                cache_dir=self._cache_dir,
                token=self._token,
            )
            text = Path(local).read_text(encoding="utf-8")
            sections = (
                parse_markdown_sections(text, spec_id=entry.spec_id, release=entry.release)
                if parse_sections
                else []
            )
            title = _extract_title(text) or entry.title
            entry_with_title = entry if title == entry.title else _replace(entry, title=title)
            yield SpecBundle(
                entry=entry_with_title,
                sections=sections,
                raw_markdown=text,
                dataset_revision=self.revision,
            )


# ---------------------- 内部小工具 ----------------------


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if "/" in path else path


def _strip_series_suffix(path: str) -> str:
    """'marked/Rel-19/38_series' → '38'; '38_series' → '38'."""
    name = _basename(path)
    return name.split("_series", 1)[0] if "_series" in name else name


def _parse_original_filename(filename: str) -> tuple[str, str | None]:
    """从 '38101-1-j50_cover.docx' 中解析 (spec_uid='38101-1', version='j50')。

    支持后缀：.docx / .doc / .zip。
    没有 version 后缀的（如 'foo.docx'）返回 (spec_uid_guess, None)。
    spec_uid 截到第一个 '-字母数字' 序列之前。
    """
    if "." not in filename:
        return "", None
    stem = filename.rsplit(".", 1)[0]
    version = parse_doc_version(filename)
    if version:
        # 去掉 '-{version}' 及其后缀，剩下 spec_uid（"38101-1" / "38211"）
        token = f"-{version}"
        idx = stem.lower().find(token)
        spec_uid = stem[:idx] if idx >= 0 else stem
    else:
        # 无版本号的情况：取主干（不含 _suffix）
        spec_uid = stem.split("_", 1)[0]
    return spec_uid, version


def _infer_spec_type(series: str) -> str:
    """目前只能从 series 大致推断；准确的 TS/TR 区分需 raw.md 标题或 original 文件名。

    GSMA marked 目录里不直接区分 TS/TR，全部混在 `NN_series/` 下。
    本期生产口径只保留 TS（5G 系列），TR 占比小且通常 series 编号相同。
    保守起见此处返回 'TS'（可被 markdown 标题解析进一步覆盖）。
    """
    if not series.isdigit():
        return "unknown"
    return "TS"


def _extract_title(text: str) -> str | None:
    """从 raw.md 第一行 H1 标题中粗略抽取 spec 全名。"""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# ") and len(line) > 2:
            return line[2:].strip()
        if line and not line.startswith("#"):
            # 没有 H1 的情况，返回首段非空行作为兜底
            return line[:200]
    return None


def _replace(entry: SpecManifestEntry, **changes) -> SpecManifestEntry:
    """SpecManifestEntry 是 frozen dataclass，更新字段需走 replace。"""
    from dataclasses import replace as _dc_replace

    return _dc_replace(entry, **changes)
