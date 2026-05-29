"""跨 spec 章节连续性 audit（read-only）。

背景：2026-05-28 用户问 "DCI1_1 的字段" 时，agent 返回的 IE hits 与期望表完全不符。
追因发现 38.212 §7.3.1.2.2 (Format 1_1) 整段从 BM25/向量 chunks 里消失。怀疑
hf_loader.markdown_parser 的 _HEADING_RE 在某些章节标题上没识别成 heading，导致
该 section 与上一个 section 的 body 粘在一起被吞掉。

本脚本扫 `/data/tgpp/bm25/voyage/chunks.jsonl`，聚合每个 spec 出现过的 clause 集合，
检测"叶子级数字跳号"：

- 如果某 spec 下出现 clause `a.b.c.X` 与 `a.b.c.Z`（X < Z），但 X+1..Z-1 全缺，则
  `a.b.c.(X+1) .. a.b.c.(Z-1)` 计为疑似缺段。
- 同样规则对每个 clause 前缀逐层做（顶层 `1 / 2 / 3 / ...`、第二层 `1.1 / 1.2 / ...`
  等）。
- 跳号 1 (相邻号缺 1 个) 也会上报，但通常 3GPP spec 偶尔确实存在「编号留空」的设计
  （比如 release 间删除 / void 章节），所以输出报告里按"缺漏数量"和"是否子节"做分级。

输出：CSV-friendly 表 + 按 spec 排序的 markdown 报告。

用法：
    uv run python -m ingestion.scripts.clause_gap_audit \
        --chunks /data/tgpp/bm25/voyage/chunks.jsonl \
        --out /tmp/clause_gap_report.md \
        --csv /tmp/clause_gap_report.csv

只读、零外部 API、纯 Python，无副作用。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

log = logging.getLogger("clause_gap_audit")

# clause 形态：`7.3.1.2.1`、`A.3.4`、`5a.2.1`（罕见 release 拓展），允许字母前缀做 Annex
# 我们只对"叶子全是纯数字"的子号做跳号检测，避免误报附录的字母编号
_LEAF_NUMERIC_RE = re.compile(r"^\d+$")


@dataclass(slots=True)
class GapEntry:
    spec_id: str
    parent_path: tuple[str, ...]  # 例如 ("7", "3", "1", "2")
    missing_leafs: list[int]  # 例如 [2]（指 7.3.1.2.2）
    surrounding: tuple[int, int]  # (前一个存在的叶子号, 后一个存在的叶子号)
    section_titles_around: tuple[str, str]  # 前后两个 clause 的 section_title


@dataclass(slots=True)
class SpecStats:
    spec_id: str
    total_clauses: int = 0
    unique_clauses: int = 0
    gaps: list[GapEntry] = field(default_factory=list)


def _split_clause(clause: str) -> tuple[str, ...] | None:
    """把 `7.3.1.2.1` 拆成 ("7","3","1","2","1")。空串 / 不规则返回 None。"""
    s = (clause or "").strip()
    if not s:
        return None
    parts = s.split(".")
    if not parts or any(not p for p in parts):
        return None
    return tuple(parts)


def _collect_clauses(chunks_path: Path) -> dict[str, dict[tuple[str, ...], str]]:
    """spec_id → {clause_tuple: section_title_first_seen}.

    一个 clause 通常会有多个 chunk（按 document_order 切的），我们记第一次见到的
    section_title 即可。section_title 用于报告里展示"缺段的前后是什么"。
    """
    by_spec: dict[str, dict[tuple[str, ...], str]] = defaultdict(dict)
    with chunks_path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            spec_id = obj.get("spec_id")
            clause = obj.get("clause")
            title = obj.get("section_title") or ""
            if not spec_id:
                continue
            parts = _split_clause(clause or "")
            if parts is None:
                continue
            slot = by_spec[spec_id]
            if parts not in slot:
                slot[parts] = title
            if line_no % 100_000 == 0:
                log.info("scanned %s lines", line_no)
    return by_spec


def _find_gaps_for_prefix(
    *,
    spec_id: str,
    parent: tuple[str, ...],
    leafs_present: list[str],
    title_lookup: dict[tuple[str, ...], str],
) -> list[GapEntry]:
    """对同一 parent 下的所有 leaf 编号做"纯数字跳号"检测。"""
    numeric_leafs: list[int] = []
    for leaf in leafs_present:
        if _LEAF_NUMERIC_RE.match(leaf):
            numeric_leafs.append(int(leaf))
    if len(numeric_leafs) < 2:
        return []
    numeric_leafs = sorted(set(numeric_leafs))
    gaps: list[GapEntry] = []
    for prev, nxt in pairwise(numeric_leafs):
        if nxt - prev <= 1:
            continue
        missing = list(range(prev + 1, nxt))
        prev_path = (*parent, str(prev))
        nxt_path = (*parent, str(nxt))
        gaps.append(
            GapEntry(
                spec_id=spec_id,
                parent_path=parent,
                missing_leafs=missing,
                surrounding=(prev, nxt),
                section_titles_around=(
                    title_lookup.get(prev_path, ""),
                    title_lookup.get(nxt_path, ""),
                ),
            )
        )
    return gaps


def _audit_spec(
    *,
    spec_id: str,
    clauses: dict[tuple[str, ...], str],
) -> SpecStats:
    stats = SpecStats(
        spec_id=spec_id,
        total_clauses=len(clauses),
        unique_clauses=len(clauses),
    )

    # 按 parent 分桶：parent_path -> [leaf, ...]
    # parent 是 clause 去掉最后一段，root 的 parent = ()
    buckets: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for path in clauses:
        if not path:
            continue
        parent = path[:-1]
        leaf = path[-1]
        buckets[parent].append(leaf)

    for parent, leafs in buckets.items():
        stats.gaps.extend(
            _find_gaps_for_prefix(
                spec_id=spec_id,
                parent=parent,
                leafs_present=leafs,
                title_lookup=clauses,
            )
        )
    return stats


def _format_gap_md(gap: GapEntry) -> str:
    parent_str = ".".join(gap.parent_path) or "(root)"
    missing_str = ", ".join(
        f"{parent_str}.{m}" if gap.parent_path else str(m) for m in gap.missing_leafs
    )
    return (
        f"  - 缺 `§{missing_str}`（在 `§{parent_str}.{gap.surrounding[0]}` "
        f"\u300c{gap.section_titles_around[0]}\u300d 与 `§{parent_str}.{gap.surrounding[1]}` "
        f"\u300c{gap.section_titles_around[1]}\u300d 之间）"
    )


def _spec_severity_sort_key(s: SpecStats) -> tuple[int, int, str]:
    # 排序优先级：缺段数（多者先） / clause 数（多者先） / spec_id
    return (-sum(len(g.missing_leafs) for g in s.gaps), -s.unique_clauses, s.spec_id)


def _is_high_priority_spec(spec_id: str) -> bool:
    """重点 spec：NR 物理层 / RRC / NAS / NGAP 等 5G 核心规范。"""
    high = {
        "38.211",
        "38.212",
        "38.213",
        "38.214",
        "38.215",
        "38.300",
        "38.321",
        "38.322",
        "38.323",
        "38.331",
        "38.401",
        "38.413",
        "38.423",
        "23.501",
        "23.502",
        "24.501",
        "29.500",
        "29.501",
        "29.502",
        "29.503",
        "29.518",
    }
    return spec_id in high


def render_report(stats_list: Iterable[SpecStats]) -> str:
    items = sorted(stats_list, key=_spec_severity_sort_key)
    items_with_gaps = [s for s in items if s.gaps]
    high = [s for s in items_with_gaps if _is_high_priority_spec(s.spec_id)]
    other = [s for s in items_with_gaps if not _is_high_priority_spec(s.spec_id)]

    total_missing = sum(sum(len(g.missing_leafs) for g in s.gaps) for s in items_with_gaps)
    parts: list[str] = []
    parts.append("# Clause Gap Audit Report")
    parts.append("")
    parts.append(
        f"扫描全量 chunks：spec={len(items)}，有缺段 spec={len(items_with_gaps)}，"
        f"总缺段数（纯数字跳号 leaf 计）={total_missing}"
    )
    parts.append("")
    parts.append("## 重点 spec（5G 核心）")
    parts.append("")
    if not high:
        parts.append("（无）")
    for s in high:
        miss_count = sum(len(g.missing_leafs) for g in s.gaps)
        parts.append(f"### `{s.spec_id}` — 缺段 {miss_count} 处 / 共 {s.unique_clauses} clause")
        for gap in s.gaps:
            parts.append(_format_gap_md(gap))
        parts.append("")
    parts.append("## 其它 spec（前 50 严重度）")
    parts.append("")
    for s in other[:50]:
        miss_count = sum(len(g.missing_leafs) for g in s.gaps)
        parts.append(f"- `{s.spec_id}`：缺段 {miss_count} 处 / 共 {s.unique_clauses} clause")
    if len(other) > 50:
        parts.append("")
        parts.append(f"（其余 {len(other) - 50} 个 spec 省略）")
    return "\n".join(parts) + "\n"


def render_csv(stats_list: Iterable[SpecStats]) -> str:
    parts: list[str] = []
    parts.append("spec_id,parent_path,missing_leaf,prev_clause,prev_title,next_clause,next_title")
    for s in sorted(stats_list, key=_spec_severity_sort_key):
        for gap in s.gaps:
            parent_str = ".".join(gap.parent_path)
            for m in gap.missing_leafs:
                missing_clause = f"{parent_str}.{m}" if parent_str else str(m)
                prev_clause = (
                    f"{parent_str}.{gap.surrounding[0]}" if parent_str else str(gap.surrounding[0])
                )
                next_clause = (
                    f"{parent_str}.{gap.surrounding[1]}" if parent_str else str(gap.surrounding[1])
                )
                row = [
                    s.spec_id,
                    parent_str,
                    missing_clause,
                    prev_clause,
                    _csv_escape(gap.section_titles_around[0]),
                    next_clause,
                    _csv_escape(gap.section_titles_around[1]),
                ]
                parts.append(",".join(row))
    return "\n".join(parts) + "\n"


def _csv_escape(s: str) -> str:
    s = s.replace('"', '""')
    if any(c in s for c in [",", '"', "\n"]):
        return f'"{s}"'
    return s


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chunks",
        type=Path,
        default=Path("/data/tgpp/bm25/voyage/chunks.jsonl"),
    )
    parser.add_argument("--out", type=Path, default=Path("/tmp/clause_gap_report.md"))
    parser.add_argument("--csv", type=Path, default=Path("/tmp/clause_gap_report.csv"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    log.info("loading chunks from %s", args.chunks)
    by_spec = _collect_clauses(args.chunks)
    log.info("loaded %d specs", len(by_spec))

    stats_list = [_audit_spec(spec_id=sid, clauses=clauses) for sid, clauses in by_spec.items()]
    args.out.write_text(render_report(stats_list), encoding="utf-8")
    args.csv.write_text(render_csv(stats_list), encoding="utf-8")
    print(f"report → {args.out}")
    print(f"csv → {args.csv}")


if __name__ == "__main__":
    main()
