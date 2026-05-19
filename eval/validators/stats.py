"""金标准分布统计（M7.0）。

口径锚 docs/03-development/06-evaluation-and-observability.md §3.4：
    definition ~30 / procedure ~35 / multi_section ~10 / table_lookup ~10 /
    formula ~10 / tool ~10 / negative ~15

外加 daily eval 子集硬要求（同 §0 决策 Q1）：source==hand_crafted 至少 20 题。

入口：`compute_stats(path, tolerance=5)` → `GoldenStats`
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# §3.4 目标分布
CATEGORY_TARGETS: dict[str, int] = {
    "definition": 30,
    "procedure": 35,
    "multi_section": 10,
    "table_lookup": 10,
    "formula": 10,
    "tool": 10,
    "negative": 15,
}

# daily eval 子集硬要求（§0 Q1）
SOURCE_TARGETS: dict[str, int] = {
    "hand_crafted": 20,
}


@dataclass(slots=True)
class CategoryRow:
    category: str
    actual: int
    target: int
    delta: int  # actual - target
    status: str  # "OK" | "GAP" (under target) | "OVER" (over target)


@dataclass(slots=True)
class SourceRow:
    source: str
    actual: int
    target: int  # 0 = 无硬要求
    status: str  # "OK" | "GAP" (under) | "INFO" (无目标)


@dataclass(slots=True)
class GoldenStats:
    file: Path
    total: int
    tolerance: int
    categories: list[CategoryRow] = field(default_factory=list)
    sources: list[SourceRow] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    unknown_categories: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            all(r.status == "OK" for r in self.categories)
            and all(r.status in ("OK", "INFO") for r in self.sources)
            and not self.unknown_categories
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": str(self.file),
            "ok": self.ok,
            "total": self.total,
            "tolerance": self.tolerance,
            "categories": [
                {
                    "category": r.category,
                    "actual": r.actual,
                    "target": r.target,
                    "delta": r.delta,
                    "status": r.status,
                }
                for r in self.categories
            ],
            "sources": [
                {"source": r.source, "actual": r.actual, "target": r.target, "status": r.status}
                for r in self.sources
            ],
            "languages": dict(self.languages),
            "unknown_categories": list(self.unknown_categories),
        }


def compute_stats(path: Path, *, tolerance: int = 5) -> GoldenStats:
    """读 YAML 算分布。文件无法读 / 无 items → total=0 + 全 GAP。"""
    stats = GoldenStats(file=path, total=0, tolerance=tolerance)
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, yaml.YAMLError):
        data = None

    items: list[Any] = []
    if isinstance(data, dict):
        raw_items = data.get("items")
        if isinstance(raw_items, list):
            items = raw_items
    stats.total = len(items)

    cat_counter: Counter[str] = Counter()
    src_counter: Counter[str] = Counter()
    lang_counter: Counter[str] = Counter()
    for it in items:
        if not isinstance(it, dict):
            continue
        cat = str(it.get("category") or "")
        if cat:
            cat_counter[cat] += 1
        src = str(it.get("source") or "")
        if src:
            src_counter[src] += 1
        lang = str(it.get("language") or "")
        if lang:
            lang_counter[lang] += 1

    # category 行（按 §3.4 目标顺序）
    for cat, target in CATEGORY_TARGETS.items():
        actual = cat_counter.get(cat, 0)
        delta = actual - target
        if abs(delta) <= tolerance:
            status = "OK"
        elif delta < 0:
            status = "GAP"
        else:
            status = "OVER"
        stats.categories.append(
            CategoryRow(category=cat, actual=actual, target=target, delta=delta, status=status)
        )

    # 未知 category（出现但不在 §3.4 目标里）
    stats.unknown_categories = sorted(set(cat_counter) - set(CATEGORY_TARGETS))

    # source 行：先列硬要求（hand_crafted），再列其他实际出现的
    for src, target in SOURCE_TARGETS.items():
        actual = src_counter.get(src, 0)
        status = "OK" if actual >= target else "GAP"
        stats.sources.append(SourceRow(source=src, actual=actual, target=target, status=status))
    for src in sorted(set(src_counter) - set(SOURCE_TARGETS)):
        stats.sources.append(
            SourceRow(source=src, actual=src_counter[src], target=0, status="INFO")
        )

    stats.languages = dict(sorted(lang_counter.items()))

    return stats


def format_stats(stats: GoldenStats) -> str:
    """terminal 友好；表格列宽固定，可在终端直接读。"""
    lines: list[str] = []
    lines.append(f"golden stats: {stats.file} — {stats.total} items (tolerance ±{stats.tolerance})")

    lines.append("")
    lines.append("Category breakdown (§3.4 targets):")
    lines.append(f"  {'category':<16}{'actual':>8}{'target':>8}{'delta':>8}  status")
    for r in stats.categories:
        sign = "+" if r.delta > 0 else ""
        lines.append(
            f"  {r.category:<16}{r.actual:>8}{r.target:>8}" f"{sign + str(r.delta):>8}  {r.status}"
        )
    if stats.unknown_categories:
        lines.append(f"  unknown categories present: {stats.unknown_categories}")

    lines.append("")
    lines.append("Source breakdown:")
    lines.append(f"  {'source':<24}{'actual':>8}{'target':>10}  status")
    for sr in stats.sources:
        target_disp = str(sr.target) if sr.target else "—"
        lines.append(f"  {sr.source:<24}{sr.actual:>8}{target_disp:>10}  {sr.status}")

    lines.append("")
    lines.append("Language breakdown:")
    if stats.languages:
        for k, v in stats.languages.items():
            lines.append(f"  {k:<6}{v:>6}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(
        "Overall: " + ("OK" if stats.ok else "FAIL (see GAP / OVER / unknown lines above)")
    )
    return "\n".join(lines)
