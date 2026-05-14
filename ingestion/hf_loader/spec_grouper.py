"""spec_uid 解析、TS 5G 系列白名单过滤、跨 release 去重保留最新。

核心约定（docs/03-development/02-ingestion-and-indexing.md §4.1, §2）：
- `spec_uid` = GSMA 目录名（紧凑形式，如 "38211" / "38101-1"）
- `spec_id`  = 对外编号（dotted，"38.211" / "38.101-1"）
- 跨 release 同 spec_id：保留最新（Rel-19 优先于 Rel-18）
- TS 5G 系列白名单（M6 生产）：
  {21,22,23,24,26,27,28,29,31,32,33,34,35,36,37,38}
"""

from __future__ import annotations

import re

# Rel-14 起 38 系列开始出现；本期生产范围在 README 中明确为 5G 相关
# TS 系列（不含 25 UTRA legacy / 41-55 legacy / TR）。
TS_5G_SERIES_WHITELIST: frozenset[str] = frozenset(
    {"21", "22", "23", "24", "26", "27", "28", "29", "31", "32", "33", "34", "35", "36", "37", "38"}
)

# Release 排序：数字越大越新；本期主路径只关心 Rel-18 / Rel-19，但保留
# 通用排序逻辑以便未来覆盖 Rel-17 兜底或 Rel-20 增量。
_REL_RE = re.compile(r"^Rel-(\d+)$")


def release_rank(release: str) -> int:
    """把 'Rel-19' 这种字符串转成可比较的整数，未知格式返回 -1。"""
    match = _REL_RE.match(release)
    return int(match.group(1)) if match else -1


# spec_uid 形态：
#   "38211"      → spec_id "38.211"
#   "38101-1"    → spec_id "38.101-1"
#   "23501"      → spec_id "23.501"
#   "23501-1"    → spec_id "23.501-1"
#
# GSMA `marked/Rel-N/NN_series/SPEC_UID/`，spec_uid 总以 series 编号开头。
_SPEC_UID_RE = re.compile(r"^(?P<series>\d{2})(?P<rest>\d+)(?P<suffix>(?:-\d+)?)$")


def parse_spec_uid(spec_uid: str) -> tuple[str, str, str]:
    """返回 (series, spec_id, normalized_uid)。

    若 spec_uid 不匹配预期形态，返回 ('', spec_uid, spec_uid)，调用方需自行兜底。
    """
    match = _SPEC_UID_RE.match(spec_uid)
    if not match:
        return ("", spec_uid, spec_uid)
    series = match.group("series")
    rest = match.group("rest")
    suffix = match.group("suffix")
    spec_id = f"{series}.{rest}{suffix}"
    return series, spec_id, spec_uid


# original/ 目录中的 docx 文件名形如：
#   "38211-j50_s00-04.docx"
#   "38101-1-j50_cover.docx"
#   "21101-j00.docx"
# 提取版本号 "j50" / "j00" 等（3GPP 文件命名约定）。
_VERSION_RE = re.compile(r"-([a-z]\d{2})(?:_|\.)", re.IGNORECASE)


def parse_doc_version(filename: str) -> str | None:
    """从 original/ 目录 docx 文件名中提取 3GPP 版本号（如 'j50'）。"""
    match = _VERSION_RE.search(filename)
    return match.group(1).lower() if match else None


def filter_ts_5g(entries: list, *, whitelist: frozenset[str] = TS_5G_SERIES_WHITELIST) -> list:
    """保留 TS 且 series 在白名单内的 entries。"""
    return [e for e in entries if e.spec_type == "TS" and e.series in whitelist]


def dedupe_keep_latest(entries: list) -> list:
    """同 spec_id 多 release 时保留 release 最新的那一条。

    顺序保留（按首次出现的位置），同 spec_id 仅保留最新版本。
    """
    best_by_spec: dict[str, object] = {}
    order: list[str] = []
    for entry in entries:
        key = entry.spec_id
        if key not in best_by_spec:
            order.append(key)
            best_by_spec[key] = entry
            continue
        current = best_by_spec[key]
        if release_rank(entry.release) > release_rank(current.release):  # type: ignore[attr-defined]
            best_by_spec[key] = entry
    return [best_by_spec[key] for key in order]
