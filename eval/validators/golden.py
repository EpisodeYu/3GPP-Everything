"""金标准 YAML 校验器（M7.0）。

口径锚 `docs/03-development/06-evaluation-and-observability.md §3.5 + §12`：
- 必填字段：id / category / language / question / expected_specs / expected_facts /
  forbidden / source / must_say_not_found
- 枚举：category ∈ §3.5 7 类；language ∈ {en, zh}；source ∈ {teleqna_transformed, hand_crafted}
- id 跨 items 全局唯一
- negative 类约束：expected_specs 必空 + must_say_not_found == true
- spec_id 形如 "23.501"（NN.NNN）；section_path 是 list[str]

错误位置 = 1-indexed YAML 行号。用 `yaml.compose` 拿 Node tree 算每个 item / 字段
的 start line（PyYAML 返回 0-index，+1 给人类）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# --- 枚举 / 必填 -----------------------------------------------------------

CATEGORY_ENUM: frozenset[str] = frozenset(
    {
        "definition",
        "procedure",
        "multi_section",
        "table_lookup",
        "formula",
        "tool",
        "negative",
    }
)
LANGUAGE_ENUM: frozenset[str] = frozenset({"en", "zh"})
SOURCE_ENUM: frozenset[str] = frozenset({"teleqna_transformed", "hand_crafted"})

# 必填字段：值不能是 None / ""；list 字段允许 [] 但 key 必须存在
_REQUIRED_ITEM_FIELDS: tuple[str, ...] = (
    "id",
    "category",
    "language",
    "question",
    "expected_specs",
    "expected_facts",
    "forbidden",
    "source",
)

# spec_id 形如 "23.501" / "38.331"；允许大类号 1-3 位 + 子号 1-3 位（覆盖 21.xxx / 33.xxx）
_SPEC_ID_RE = re.compile(r"^\d{1,3}\.\d{1,3}$")


# --- dataclass -------------------------------------------------------------


@dataclass(slots=True)
class ValidationIssue:
    """单条校验问题。severity='error' 一律阻塞通过；'warning' 仅提示。"""

    severity: str
    code: str
    message: str
    location: str
    line: int | None = None
    item_id: str | None = None


@dataclass(slots=True)
class ValidationReport:
    file: Path | None
    total_items: int
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": str(self.file) if self.file else None,
            "ok": self.ok,
            "total_items": self.total_items,
            "errors": [_issue_to_dict(e) for e in self.errors],
            "warnings": [_issue_to_dict(w) for w in self.warnings],
        }


def _issue_to_dict(i: ValidationIssue) -> dict[str, Any]:
    return {
        "severity": i.severity,
        "code": i.code,
        "message": i.message,
        "location": i.location,
        "line": i.line,
        "item_id": i.item_id,
    }


# --- 行号映射 --------------------------------------------------------------


def _build_line_map(text: str) -> dict[tuple[Any, ...], int]:
    """compose YAML → {("items", idx): line, ("items", idx, key): line}。

    1-indexed。compose 失败时返回空 map，错误用 0 行号兜底（不阻塞 schema 校验）。
    """
    line_map: dict[tuple[Any, ...], int] = {}
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return line_map
    if not isinstance(root, yaml.MappingNode):
        return line_map
    for key_node, value_node in root.value:
        key = getattr(key_node, "value", None)
        if key == "items" and isinstance(value_node, yaml.SequenceNode):
            for idx, item_node in enumerate(value_node.value):
                line_map[("items", idx)] = item_node.start_mark.line + 1
                if isinstance(item_node, yaml.MappingNode):
                    for ikey_node, _ivalue_node in item_node.value:
                        ikey = getattr(ikey_node, "value", None)
                        if ikey is None:
                            continue
                        line_map[("items", idx, ikey)] = ikey_node.start_mark.line + 1
    return line_map


# --- 公共入口 --------------------------------------------------------------


def validate_golden_file(path: Path) -> ValidationReport:
    """读文件并校验。文件不存在 / 不可读 → 报告级 error。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report = ValidationReport(file=path, total_items=0)
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="file_not_readable",
                message=f"无法读取文件：{exc}",
                location="<file>",
                line=None,
            )
        )
        return report
    rep = validate_golden_text(text)
    rep.file = path
    return rep


def validate_golden_text(text: str) -> ValidationReport:
    """对 YAML 文本做校验。便于单测 + CLI 都能跑同一条路径。"""
    report = ValidationReport(file=None, total_items=0)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML 的 YAMLError 通常带 problem_mark
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else None
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="yaml_parse_error",
                message=f"YAML 解析失败：{exc}",
                location="<root>",
                line=line,
            )
        )
        return report

    if not isinstance(data, dict):
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="root_not_mapping",
                message="顶层必须是 mapping（dict），不能是 list / scalar",
                location="<root>",
                line=1,
            )
        )
        return report

    line_map = _build_line_map(text)
    _check_top_level(data, line_map, report)

    items = data.get("items")
    if not isinstance(items, list):
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="items_not_list",
                message="`items` 必须是 list",
                location="items",
                line=None,
            )
        )
        return report

    report.total_items = len(items)
    seen_ids: dict[str, int] = {}
    for idx, item in enumerate(items):
        _check_item(idx, item, line_map, seen_ids, report)

    return report


# --- 顶层校验 --------------------------------------------------------------


def _check_top_level(
    data: dict[str, Any],
    _line_map: dict[tuple[Any, ...], int],
    report: ValidationReport,
) -> None:
    if "items" not in data:
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="missing_items",
                message="顶层缺 `items` 字段",
                location="<root>",
                line=None,
            )
        )

    version = data.get("version")
    if version is None:
        report.warnings.append(
            ValidationIssue(
                severity="warning",
                code="missing_version",
                message="顶层缺 `version` 字段（建议写 1）",
                location="version",
            )
        )


# --- 单 item 校验 ----------------------------------------------------------


def _check_item(
    idx: int,
    item: Any,
    line_map: dict[tuple[Any, ...], int],
    seen_ids: dict[str, int],
    report: ValidationReport,
) -> None:
    loc_prefix = f"items[{idx}]"
    item_line = line_map.get(("items", idx))

    if not isinstance(item, dict):
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="item_not_mapping",
                message=f"第 {idx} 个 item 不是 mapping",
                location=loc_prefix,
                line=item_line,
            )
        )
        return

    item_id_raw = item.get("id")
    item_id = str(item_id_raw) if item_id_raw is not None else None

    # 必填字段（list 字段允许空数组）
    for fname in _REQUIRED_ITEM_FIELDS:
        if fname not in item:
            report.errors.append(
                ValidationIssue(
                    severity="error",
                    code="missing_field",
                    message=f"缺必填字段 `{fname}`",
                    location=f"{loc_prefix}.{fname}",
                    line=item_line,
                    item_id=item_id,
                )
            )
            continue
        v = item[fname]
        if fname in ("expected_specs", "expected_facts", "forbidden"):
            if not isinstance(v, list):
                report.errors.append(
                    ValidationIssue(
                        severity="error",
                        code="field_not_list",
                        message=f"字段 `{fname}` 必须是 list",
                        location=f"{loc_prefix}.{fname}",
                        line=line_map.get(("items", idx, fname), item_line),
                        item_id=item_id,
                    )
                )
        else:
            if v is None or (isinstance(v, str) and not v.strip()):
                report.errors.append(
                    ValidationIssue(
                        severity="error",
                        code="empty_field",
                        message=f"字段 `{fname}` 不能为空",
                        location=f"{loc_prefix}.{fname}",
                        line=line_map.get(("items", idx, fname), item_line),
                        item_id=item_id,
                    )
                )

    # id 唯一性
    if item_id:
        prev = seen_ids.get(item_id)
        if prev is not None:
            report.errors.append(
                ValidationIssue(
                    severity="error",
                    code="duplicate_id",
                    message=f"id `{item_id}` 重复（首次在 items[{prev}]）",
                    location=f"{loc_prefix}.id",
                    line=line_map.get(("items", idx, "id"), item_line),
                    item_id=item_id,
                )
            )
        else:
            seen_ids[item_id] = idx

    # 枚举：category / language / source
    cat = item.get("category")
    if cat is not None and cat not in CATEGORY_ENUM:
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="invalid_category",
                message=f"category=`{cat}` 不在枚举：{sorted(CATEGORY_ENUM)}",
                location=f"{loc_prefix}.category",
                line=line_map.get(("items", idx, "category"), item_line),
                item_id=item_id,
            )
        )

    lang = item.get("language")
    if lang is not None and lang not in LANGUAGE_ENUM:
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="invalid_language",
                message=f"language=`{lang}` 不在枚举：{sorted(LANGUAGE_ENUM)}",
                location=f"{loc_prefix}.language",
                line=line_map.get(("items", idx, "language"), item_line),
                item_id=item_id,
            )
        )

    src = item.get("source")
    if src is not None and src not in SOURCE_ENUM:
        # source 历史上有些值（如空字符串、teleqna_v1 别名），仅 warning 不阻塞 v1 既有数据
        report.warnings.append(
            ValidationIssue(
                severity="warning",
                code="unknown_source",
                message=f"source=`{src}` 不在推荐枚举：{sorted(SOURCE_ENUM)}",
                location=f"{loc_prefix}.source",
                line=line_map.get(("items", idx, "source"), item_line),
                item_id=item_id,
            )
        )

    # expected_specs 内嵌结构
    _check_expected_specs(idx, item, line_map, report, item_id)

    # negative 约束
    _check_negative(idx, item, line_map, report, item_id)


def _check_expected_specs(
    idx: int,
    item: dict[str, Any],
    line_map: dict[tuple[Any, ...], int],
    report: ValidationReport,
    item_id: str | None,
) -> None:
    specs = item.get("expected_specs")
    if not isinstance(specs, list):
        return  # 已在必填里报过
    for j, s in enumerate(specs):
        sloc = f"items[{idx}].expected_specs[{j}]"
        item_line = line_map.get(("items", idx, "expected_specs"))
        if not isinstance(s, dict):
            report.errors.append(
                ValidationIssue(
                    severity="error",
                    code="spec_not_mapping",
                    message="expected_specs 每项必须是 {spec_id, sections}",
                    location=sloc,
                    line=item_line,
                    item_id=item_id,
                )
            )
            continue
        spec_id = s.get("spec_id")
        if not spec_id or not isinstance(spec_id, str):
            report.errors.append(
                ValidationIssue(
                    severity="error",
                    code="missing_spec_id",
                    message="expected_specs 项缺 `spec_id`",
                    location=f"{sloc}.spec_id",
                    line=item_line,
                    item_id=item_id,
                )
            )
        elif not _SPEC_ID_RE.match(spec_id):
            report.warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="spec_id_format",
                    message=f"spec_id=`{spec_id}` 不像 3GPP 规范号（NN.NNN）",
                    location=f"{sloc}.spec_id",
                    line=item_line,
                    item_id=item_id,
                )
            )
        sections = s.get("sections")
        if sections is not None and not isinstance(sections, list):
            report.errors.append(
                ValidationIssue(
                    severity="error",
                    code="sections_not_list",
                    message="`sections` 必须是 list[str]",
                    location=f"{sloc}.sections",
                    line=item_line,
                    item_id=item_id,
                )
            )
        elif isinstance(sections, list):
            for k, sec in enumerate(sections):
                if not isinstance(sec, (str, int, float)):
                    report.errors.append(
                        ValidationIssue(
                            severity="error",
                            code="section_not_scalar",
                            message=f"sections[{k}] 必须是 scalar（str / number）",
                            location=f"{sloc}.sections[{k}]",
                            line=item_line,
                            item_id=item_id,
                        )
                    )


def _check_negative(
    idx: int,
    item: dict[str, Any],
    line_map: dict[tuple[Any, ...], int],
    report: ValidationReport,
    item_id: str | None,
) -> None:
    if item.get("category") != "negative":
        # 非 negative 不应声明 must_say_not_found=true（语义混淆）→ warning
        if item.get("must_say_not_found"):
            report.warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="non_negative_must_not_found",
                    message="非 negative 类不应 must_say_not_found=true",
                    location=f"items[{idx}].must_say_not_found",
                    line=line_map.get(("items", idx, "must_say_not_found")),
                    item_id=item_id,
                )
            )
        return

    # negative 类约束
    specs = item.get("expected_specs")
    if isinstance(specs, list) and specs:
        # warning（不阻塞）：teleqna 转化的 negative 历史上会保留"用来解释为何找不到"的 spec
        # 引用；模板对手写题要求 expected_specs=[]，但既有 v1.yaml 数据不应被一刀切 fail。
        report.warnings.append(
            ValidationIssue(
                severity="warning",
                code="negative_has_specs",
                message=(
                    "negative 题建议 expected_specs=[]（手写题严格要求；" "teleqna 转化可保留引用）"
                ),
                location=f"items[{idx}].expected_specs",
                line=line_map.get(("items", idx, "expected_specs")),
                item_id=item_id,
            )
        )
    if not item.get("must_say_not_found"):
        report.errors.append(
            ValidationIssue(
                severity="error",
                code="negative_must_say_not_found",
                message="negative 题必须 must_say_not_found: true",
                location=f"items[{idx}].must_say_not_found",
                line=line_map.get(("items", idx, "must_say_not_found")),
                item_id=item_id,
            )
        )


# --- 报告渲染 --------------------------------------------------------------


def format_report(report: ValidationReport) -> str:
    """terminal-friendly 文本格式；CLI 用。"""
    lines: list[str] = []
    head = f"golden validate: {report.file or '<text>'} — {report.total_items} items"
    lines.append(head)
    if report.ok:
        lines.append(f"  OK ({len(report.warnings)} warning)")
    else:
        lines.append(f"  FAIL ({len(report.errors)} error, {len(report.warnings)} warning)")
    for it in report.errors:
        lines.append(_fmt_issue(it))
    for it in report.warnings:
        lines.append(_fmt_issue(it))
    return "\n".join(lines)


def _fmt_issue(it: ValidationIssue) -> str:
    where = f"L{it.line}" if it.line is not None else "L?"
    badge = "ERROR" if it.severity == "error" else "WARN "
    iid = f" [{it.item_id}]" if it.item_id else ""
    return f"  {badge} {where} {it.location}{iid}  {it.code}: {it.message}"
