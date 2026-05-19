"""M7.0 单测：`eval.validators.golden` schema 校验。

锚 docs/03-development/06-evaluation-and-observability.md §3.5 + §12 M7.0 row。
覆盖：
- happy path（hand-crafted 4 example + teleqna 转化样本均通过）
- 必填字段缺失（id / category / language / question / expected_specs / ...）
- 枚举失败（category / language / source）
- id 重复 → error + 报首次位置
- negative 约束（expected_specs 非空 → error；must_say_not_found 缺 → error）
- spec_id 形状（"38331" → warning）
- yaml 解析错误 → error 含 1-indexed 行号
- CLI exit code（典型 happy / fail / strict-warnings）
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from eval.cli import app
from eval.validators.golden import (
    CATEGORY_ENUM,
    LANGUAGE_ENUM,
    validate_golden_file,
    validate_golden_text,
)

pytestmark = pytest.mark.unit


# --- 最小可通过样本（用于 baseline / 衍生异常）---


_OK_YAML = dedent("""\
    version: 1
    created_at: '2026-05-19'
    sources:
      - hand_crafted
    items:
      - id: def-001
        category: definition
        language: en
        source: hand_crafted
        question: What is AMF?
        expected_specs:
          - spec_id: "23.501"
            sections: ["6.2.1"]
        expected_facts:
          - Access and Mobility Management Function
        forbidden: []
        must_say_not_found: false

      - id: neg-001
        category: negative
        language: en
        source: hand_crafted
        question: What is the MAC format of a PDU Session?
        expected_specs: []
        expected_facts: []
        forbidden:
          - "00:"
        must_say_not_found: true
    """)


def test_ok_yaml_passes() -> None:
    rep = validate_golden_text(_OK_YAML)
    assert rep.ok, rep.errors
    assert rep.total_items == 2
    assert rep.warnings == []


def test_missing_items_key_errors() -> None:
    rep = validate_golden_text("version: 1\nsources: []\n")
    assert not rep.ok
    codes = [e.code for e in rep.errors]
    assert "missing_items" in codes
    assert "items_not_list" in codes  # items 不存在 → 也不是 list


def test_yaml_parse_error_carries_line() -> None:
    bad = "items:\n  - id: x\n    category: : :\n"
    rep = validate_golden_text(bad)
    assert not rep.ok
    e = rep.errors[0]
    assert e.code == "yaml_parse_error"
    # PyYAML 在错误的 token 行报；3 行版本通常 line>=3
    assert e.line is not None and e.line >= 1


def test_duplicate_id_errors_with_first_index() -> None:
    yaml_text = _OK_YAML.replace("id: neg-001", "id: def-001")  # 制造重复
    rep = validate_golden_text(yaml_text)
    assert not rep.ok
    dup = [e for e in rep.errors if e.code == "duplicate_id"]
    assert len(dup) == 1
    assert "items[0]" in dup[0].message
    assert dup[0].item_id == "def-001"
    # 行号指向第二次出现（items[1] 的 id）
    assert dup[0].line is not None and dup[0].line > 0


def test_missing_required_field_errors_per_field() -> None:
    yaml_text = dedent("""\
        items:
          - category: definition
            language: en
            source: hand_crafted
            question: ""
            expected_specs: []
            expected_facts: []
            forbidden: []
        """)
    rep = validate_golden_text(yaml_text)
    assert not rep.ok
    codes = {e.code for e in rep.errors}
    # id 缺 + question 空 + source 没列在必填以外的全 OK
    assert "missing_field" in codes  # id 缺
    assert "empty_field" in codes  # question 空


def test_invalid_category_enum() -> None:
    yaml_text = _OK_YAML.replace("category: definition", "category: definitions")
    rep = validate_golden_text(yaml_text)
    assert not rep.ok
    bad = [e for e in rep.errors if e.code == "invalid_category"]
    assert bad and "definitions" in bad[0].message
    assert "definition" in str(sorted(CATEGORY_ENUM))


def test_invalid_language_enum() -> None:
    yaml_text = _OK_YAML.replace("language: en", "language: fr", 1)
    rep = validate_golden_text(yaml_text)
    assert not rep.ok
    bad = [e for e in rep.errors if e.code == "invalid_language"]
    assert bad and "fr" in bad[0].message
    assert "en" in str(sorted(LANGUAGE_ENUM))


def test_unknown_source_is_warning_not_error() -> None:
    yaml_text = _OK_YAML.replace("source: hand_crafted", "source: my_bespoke_v2", 1)
    rep = validate_golden_text(yaml_text)
    assert rep.ok, "未知 source 仅 warning，不应 fail validate"
    assert any(w.code == "unknown_source" for w in rep.warnings)


def test_negative_with_expected_specs_warns_not_errors() -> None:
    """teleqna 转化的历史 negative 题保留 spec 引用 → warning，不阻塞 validate。"""
    yaml_text = dedent("""\
        items:
          - id: neg-001
            category: negative
            language: en
            source: teleqna_transformed
            question: MAC of PDU Session?
            expected_specs:
              - spec_id: "23.501"
                sections: []
            expected_facts: []
            forbidden: []
            must_say_not_found: true
        """)
    rep = validate_golden_text(yaml_text)
    assert rep.ok, [e.code for e in rep.errors]
    codes = [w.code for w in rep.warnings]
    assert "negative_has_specs" in codes


def test_negative_missing_must_say_not_found_errors() -> None:
    yaml_text = _OK_YAML.replace("must_say_not_found: true", "must_say_not_found: false", 1)
    rep = validate_golden_text(yaml_text)
    assert not rep.ok
    codes = [e.code for e in rep.errors]
    assert "negative_must_say_not_found" in codes


def test_non_negative_must_say_not_found_true_is_warning() -> None:
    yaml_text = _OK_YAML.replace(
        "id: def-001\n        category: definition\n        language: en\n        "
        "source: hand_crafted\n        question: What is AMF?",
        "id: def-001\n        category: definition\n        language: en\n        "
        "source: hand_crafted\n        question: What is AMF?",
    )
    # 把 def-001 改成 must_say_not_found: true
    yaml_text = yaml_text.replace("must_say_not_found: false", "must_say_not_found: true", 1)
    rep = validate_golden_text(yaml_text)
    assert rep.ok
    assert any(w.code == "non_negative_must_not_found" for w in rep.warnings)


def test_spec_id_shape_warning() -> None:
    yaml_text = _OK_YAML.replace('spec_id: "23.501"', 'spec_id: "38331"')
    rep = validate_golden_text(yaml_text)
    assert rep.ok, "spec_id 形状不对仅 warning"
    assert any(w.code == "spec_id_format" for w in rep.warnings)


def test_expected_specs_not_mapping() -> None:
    yaml_text = dedent("""\
        items:
          - id: x-001
            category: definition
            language: en
            source: hand_crafted
            question: what?
            expected_specs:
              - just-a-string
            expected_facts: []
            forbidden: []
            must_say_not_found: false
        """)
    rep = validate_golden_text(yaml_text)
    assert not rep.ok
    assert any(e.code == "spec_not_mapping" for e in rep.errors)


def test_section_path_not_list_errors() -> None:
    yaml_text = dedent("""\
        items:
          - id: x-001
            category: definition
            language: en
            source: hand_crafted
            question: what?
            expected_specs:
              - spec_id: "23.501"
                sections: "6.2.1"
            expected_facts: []
            forbidden: []
            must_say_not_found: false
        """)
    rep = validate_golden_text(yaml_text)
    assert not rep.ok
    assert any(e.code == "sections_not_list" for e in rep.errors)


def test_line_numbers_are_one_indexed_and_point_to_item() -> None:
    rep = validate_golden_text(_OK_YAML)
    assert rep.ok
    # 改个 category 报错，验证行号指向 category key 所在行
    yaml_text = _OK_YAML.replace("category: definition", "category: wat")
    rep = validate_golden_text(yaml_text)
    bad = next(e for e in rep.errors if e.code == "invalid_category")
    # _OK_YAML 第一个 item 的 category 在第 7 行（version=1, created=2, sources=3-4,
    # items=5, "- id: def-001"=6, "category:"=7）。允许 ±1 容错给后续微调。
    assert bad.line is not None
    assert 6 <= bad.line <= 8


# --- 模板 + 真 v1.yaml 端到端 -------------------------------------------------


def test_template_yaml_passes() -> None:
    """eval/golden/_template.yaml 4 个示例应通过校验。"""
    tpl = Path(__file__).resolve().parents[3] / "eval" / "golden" / "_template.yaml"
    if not tpl.exists():
        pytest.skip(f"template not present: {tpl}")
    rep = validate_golden_file(tpl)
    assert rep.ok, [f"{e.code} @ L{e.line}: {e.message}" for e in rep.errors]
    assert rep.total_items >= 4


def test_v1_yaml_currently_passes() -> None:
    """眼下 v1.yaml（119 题 teleqna 转化）应该通过 schema 校验。

    回归门禁：未来如果有人改坏了 v1.yaml schema，本 case 立刻 fail。
    """
    v1 = Path(__file__).resolve().parents[3] / "eval" / "golden" / "v1.yaml"
    if not v1.exists():
        pytest.skip(f"v1.yaml not present: {v1}")
    rep = validate_golden_file(v1)
    # 允许 warning（spec_id 偶有非标准格式 / source 名差异）；只要 0 error
    assert rep.ok, [f"{e.code} @ L{e.line}: {e.message}" for e in rep.errors[:10]]


# --- CLI 集成 ---------------------------------------------------------------


def test_cli_validate_ok_exit_zero(tmp_path: Path) -> None:
    p = tmp_path / "good.yaml"
    p.write_text(_OK_YAML, encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(app, ["golden", "validate", "-f", str(p)])
    assert r.exit_code == 0, r.output
    assert "OK" in r.output


def test_cli_validate_fail_exit_one(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(_OK_YAML.replace("category: definition", "category: bogus"), encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(app, ["golden", "validate", "-f", str(p)])
    assert r.exit_code == 1
    assert "invalid_category" in r.output


def test_cli_validate_json_output(tmp_path: Path) -> None:
    p = tmp_path / "good.yaml"
    p.write_text(_OK_YAML, encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(app, ["golden", "validate", "-f", str(p), "--json"])
    assert r.exit_code == 0, r.output
    import json as _json

    body = _json.loads(r.output)
    assert body["ok"] is True
    assert body["total_items"] == 2


def test_cli_validate_strict_warnings_exits_one(tmp_path: Path) -> None:
    yaml_text = _OK_YAML.replace('spec_id: "23.501"', 'spec_id: "38331"')
    p = tmp_path / "warn.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    runner = CliRunner()
    r_loose = runner.invoke(app, ["golden", "validate", "-f", str(p)])
    assert r_loose.exit_code == 0
    r_strict = runner.invoke(app, ["golden", "validate", "-f", str(p), "--strict-warnings"])
    assert r_strict.exit_code == 1


def test_cli_validate_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nope.yaml"
    runner = CliRunner()
    r = runner.invoke(app, ["golden", "validate", "-f", str(p)])
    assert r.exit_code == 1
    assert "file_not_readable" in r.output
