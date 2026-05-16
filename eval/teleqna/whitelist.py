"""17 篇 POC spec whitelist + 别名归一化。

来源：M2 POC 17 篇索引完成的 spec_id 列表
（eval-results/m2-poc/17specs_throughput.md §2 表）

别名说明：TeleQnA 数据集发布于 2023，部分 spec 引用可能用旧编号或前缀变体。
本模块提供归一化能力（去前缀 "TS "、去后缀版本号），让 filter
能稳定命中 whitelist。

如需扩展（如 23.501 → 23.501-h60 这种带版本号的别名），改 SPEC_ALIASES。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import yaml

POC_17_SPECS: frozenset[str] = frozenset(
    {
        "23.401",
        "23.501",
        "23.502",
        "23.503",
        "24.501",
        "29.500",
        "29.501",
        "29.502",
        "29.503",
        "29.518",
        "36.213",
        "38.214",
        "38.300",
        "38.331",
        "38.401",
        "38.413",
        "38.473",
    }
)

SPEC_ALIAS_PATH = Path(__file__).parent / "teleqna_spec_alias.yaml"

# 匹配 "TS 38.331", "ts38.331", "3GPP TS 38.331 Release 17", "38.331 v17.5.0", "TS38.331-h60"
# 兼容各种前缀 / 空格 / 版本号后缀。
_SPEC_ID_RE = re.compile(
    r"""
    (?:^|\b)
    (?:3GPP\s+)?
    (?:TS\s*|ts\s*)?
    (\d{2}\.\d{3})
    (?:[-\s][a-zA-Z]?\d+)?
    (?:\b|$)
    """,
    re.VERBOSE,
)


def normalize_spec_id(raw: str) -> str | None:
    """从字符串中抽取首个合法 spec_id（NN.NNN 格式）；无 → None。

    示例：
      "TS 38.331 Release 17" → "38.331"
      "ts38.331-h60" → "38.331"
      "3gpp 23.501 v17" → "23.501"
      "no spec here" → None
    """
    if not raw:
        return None
    m = _SPEC_ID_RE.search(raw)
    if not m:
        return None
    return m.group(1)


def extract_all_spec_ids(text: str) -> list[str]:
    """从一段文本中抽取所有合法 spec_id（去重保序）。

    用于 filter：扫 explanation / question 文本中的 spec 引用。
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _SPEC_ID_RE.finditer(text):
        sid = m.group(1)
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def load_spec_aliases(path: Path | None = None) -> dict[str, str]:
    """从 yaml 加载 alias → canonical_spec_id 映射。文件不存在 → 空 dict。"""
    p = path or SPEC_ALIAS_PATH
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    aliases = data.get("aliases", {}) if isinstance(data, dict) else {}
    return {str(k): str(v) for k, v in aliases.items()}


def is_in_whitelist(spec_id: str | None, *, whitelist: Iterable[str] = POC_17_SPECS) -> bool:
    """精确匹配 whitelist；caller 通常先 `normalize_spec_id` 再调本函数。"""
    if not spec_id:
        return False
    return spec_id in set(whitelist)


__all__ = [
    "POC_17_SPECS",
    "SPEC_ALIAS_PATH",
    "extract_all_spec_ids",
    "is_in_whitelist",
    "load_spec_aliases",
    "normalize_spec_id",
]
