"""金标准 YAML 合并器（M7.0）。

口径：把多个 v1.*.yaml 输入合并成单个 v1.yaml；前置条件 = 每个输入都先过
`validate_golden_file`；合并阶段额外做"跨文件 id 唯一性"校验。

入口：`merge_golden_files(inputs: Sequence[Path], output: Path) -> MergeReport`

输出 YAML 结构：
- 顶层 metadata：取第一个输入文件的 metadata（version / created_at / sources / categories）
  - `sources` 合并为各输入 sources 的并集
  - `total` 自动重算（合并后 len(items)）
- `items`：按输入顺序拼接

合并语义：collision 一律 error，不做覆盖 / 后赢——避免静默覆盖 reviewer 的修改。
调用方可加 `--force` 让"后赢"，本模块只暴露纯合并行为，flag 由 CLI 层管。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .golden import (
    ValidationIssue,
    ValidationReport,
    _build_line_map,
    validate_golden_file,
)


@dataclass(slots=True)
class MergeReport:
    """合并结果摘要；CLI / 调用方读 `ok` 决定是否真正写出。"""

    inputs: list[Path]
    output: Path | None
    written: bool = False
    total_items: int = 0
    per_file_validation: list[ValidationReport] = field(default_factory=list)
    cross_file_errors: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.cross_file_errors:
            return False
        return all(r.ok for r in self.per_file_validation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": [str(p) for p in self.inputs],
            "output": str(self.output) if self.output else None,
            "written": self.written,
            "ok": self.ok,
            "total_items": self.total_items,
            "per_file_validation": [r.to_dict() for r in self.per_file_validation],
            "cross_file_errors": [
                {
                    "severity": e.severity,
                    "code": e.code,
                    "message": e.message,
                    "location": e.location,
                    "line": e.line,
                    "item_id": e.item_id,
                }
                for e in self.cross_file_errors
            ],
        }


def merge_golden_files(
    inputs: list[Path] | tuple[Path, ...],
    output: Path | None,
    *,
    dry_run: bool = False,
    force_overlap: bool = False,
) -> MergeReport:
    """合并多个金标准 YAML。

    Args:
        inputs: 至少 1 个；空 list → ValueError
        output: 写入路径；`dry_run=True` 时只校验不写
        force_overlap: 允许 id 重复（"后赢"语义）；默认 False，遇 collision 即 error
    """
    if not inputs:
        raise ValueError("merge_golden_files: inputs 不能为空")
    inputs = list(inputs)

    report = MergeReport(inputs=inputs, output=output)

    # 1. 每个输入先过 validate（任一 error → 不进合并阶段）
    loaded: list[tuple[Path, dict[str, Any], dict[tuple[Any, ...], int]]] = []
    for p in inputs:
        v = validate_golden_file(p)
        report.per_file_validation.append(v)
        if not v.ok:
            continue
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        line_map = _build_line_map(text)
        loaded.append((p, data, line_map))

    if not report.ok:
        # 任一输入 invalid → 不继续；report.ok 已是 False
        return report

    # 2. 跨文件 id 唯一性
    seen: dict[str, tuple[Path, int, int | None]] = {}  # id → (path, idx, line)
    for path, data, line_map in loaded:
        for idx, item in enumerate(data.get("items") or []):
            if not isinstance(item, dict):
                continue
            iid = item.get("id")
            if not isinstance(iid, str):
                continue
            line = line_map.get(("items", idx, "id"), line_map.get(("items", idx)))
            if iid in seen and not force_overlap:
                first_path, first_idx, first_line = seen[iid]
                report.cross_file_errors.append(
                    ValidationIssue(
                        severity="error",
                        code="cross_file_duplicate_id",
                        message=(
                            f"id `{iid}` 在 {path.name} 与 {first_path.name} 都出现"
                            f"（首次：{first_path.name}:L{first_line} items[{first_idx}]）"
                        ),
                        location=f"{path}::items[{idx}].id",
                        line=line,
                        item_id=iid,
                    )
                )
            else:
                seen[iid] = (path, idx, line)

    if not report.ok:
        return report

    # 3. 实际合并
    merged_items: list[Any] = []
    seen_ids_in_output: set[str] = set()
    for _path, data, _line_map in loaded:
        for item in data.get("items") or []:
            if isinstance(item, dict):
                iid = item.get("id")
                if force_overlap and isinstance(iid, str) and iid in seen_ids_in_output:
                    # force：后赢 → 替换已落入的
                    merged_items = [
                        x for x in merged_items if not (isinstance(x, dict) and x.get("id") == iid)
                    ]
                if isinstance(iid, str):
                    seen_ids_in_output.add(iid)
            merged_items.append(item)

    head_data = loaded[0][1]
    sources_union: list[str] = []
    seen_sources: set[str] = set()
    for _p, data, _lm in loaded:
        for s in data.get("sources") or []:
            s_str = str(s)
            if s_str not in seen_sources:
                seen_sources.add(s_str)
                sources_union.append(s_str)
    categories_union: list[str] = []
    seen_cats: set[str] = set()
    for _p, data, _lm in loaded:
        for c in data.get("categories") or []:
            c_str = str(c)
            if c_str not in seen_cats:
                seen_cats.add(c_str)
                categories_union.append(c_str)

    merged: dict[str, Any] = {
        "version": head_data.get("version", 1),
    }
    if "created_at" in head_data:
        merged["created_at"] = head_data["created_at"]
    merged["total"] = len(merged_items)
    if sources_union:
        merged["sources"] = sources_union
    if categories_union:
        merged["categories"] = categories_union
    merged["items"] = merged_items

    report.total_items = len(merged_items)

    # 4. 写出（dry_run 跳过）
    if output is not None and not dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                merged,
                f,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
        report.written = True

    return report


def format_merge_report(report: MergeReport) -> str:
    """terminal 友好；CLI 用。"""
    lines: list[str] = []
    head = (
        f"golden merge: {' + '.join(p.name for p in report.inputs)} "
        f"→ {report.output.name if report.output else '<dry-run>'}"
    )
    lines.append(head)
    for v in report.per_file_validation:
        status = "OK" if v.ok else f"FAIL ({len(v.errors)} error)"
        lines.append(
            f"  input {v.file.name if v.file else '?'}: "
            f"{v.total_items} items — {status}"
            + (f", {len(v.warnings)} warning" if v.warnings else "")
        )
        for e in v.errors[:10]:
            lines.append(f"    ERROR L{e.line} {e.location}: {e.code} — {e.message}")

    if report.cross_file_errors:
        lines.append(f"  cross-file: {len(report.cross_file_errors)} error")
        for e in report.cross_file_errors[:20]:
            lines.append(f"    ERROR L{e.line} {e.location}: {e.code} — {e.message}")
    if report.ok:
        action = "would write" if not report.written else "wrote"
        lines.append(f"  merged: {report.total_items} items — {action}")
    else:
        lines.append(f"  merged: SKIPPED (not ok; total would be {report.total_items})")
    return "\n".join(lines)
