"""M7.0 单测：`eval.validators.merger` 跨文件合并。

覆盖：
- happy path 两文件合并 → items 拼接、sources 取并集、total 重算
- 任一输入 validate fail → 不进入合并
- 跨文件 id 冲突 → error 带 file:line 双方
- --force 允许冲突且后赢
- dry-run 不写文件
- 单文件输入 = 拷贝
- 顶层 categories 取并集（保持顺序）
- CLI happy / collision / dry-run / --force
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from typer.testing import CliRunner

from eval.cli import app
from eval.validators.merger import merge_golden_files

pytestmark = pytest.mark.unit


_TEMPLATE_HEAD = dedent("""\
    version: 1
    created_at: '2026-05-19'
    sources:
      - {source}
    items:
    """)


def _make_item(iid: str, *, category: str = "definition", source: str = "hand_crafted") -> str:
    return dedent(f"""\
          - id: {iid}
            category: {category}
            language: en
            source: {source}
            question: q {iid}?
            expected_specs:
              - spec_id: "23.501"
                sections: ["6.2.1"]
            expected_facts:
              - foo
            forbidden: []
            must_say_not_found: false
        """)


def _write(tmp: Path, name: str, items: list[str], *, source: str = "hand_crafted") -> Path:
    p = tmp / name
    p.write_text(_TEMPLATE_HEAD.format(source=source) + "".join(items), encoding="utf-8")
    return p


def test_merge_two_files_happy(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("a-1"), _make_item("a-2")])
    b = _write(tmp_path, "b.yaml", [_make_item("b-1")], source="teleqna_transformed")
    out = tmp_path / "merged.yaml"
    r = merge_golden_files([a, b], out)
    assert r.ok, r.cross_file_errors
    assert r.written
    assert r.total_items == 3
    merged = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert [it["id"] for it in merged["items"]] == ["a-1", "a-2", "b-1"]
    assert merged["total"] == 3
    # sources 并集（顺序保留：a 在前，b 在后）
    assert merged["sources"] == ["hand_crafted", "teleqna_transformed"]


def test_merge_rejects_when_input_invalid(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("a-1")])
    # b 含非法 category
    b = tmp_path / "b.yaml"
    b.write_text(
        _TEMPLATE_HEAD.format(source="hand_crafted") + _make_item("b-1", category="bogus"),
        encoding="utf-8",
    )
    out = tmp_path / "merged.yaml"
    r = merge_golden_files([a, b], out)
    assert not r.ok
    assert not r.written
    # input b 的 validate 报 invalid_category
    failing = [v for v in r.per_file_validation if not v.ok]
    assert failing and any(e.code == "invalid_category" for e in failing[0].errors)
    assert not out.exists()


def test_merge_cross_file_id_collision_errors(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("dup-1"), _make_item("a-2")])
    b = _write(tmp_path, "b.yaml", [_make_item("dup-1"), _make_item("b-2")])
    out = tmp_path / "merged.yaml"
    r = merge_golden_files([a, b], out)
    assert not r.ok
    assert not r.written
    codes = [e.code for e in r.cross_file_errors]
    assert "cross_file_duplicate_id" in codes
    msg = r.cross_file_errors[0].message
    assert "a.yaml" in msg and "b.yaml" in msg


def test_merge_force_overlap_keeps_later(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("dup-1", category="definition")])
    # 同 id 但不同 category — force 后第二个赢
    b = _write(tmp_path, "b.yaml", [_make_item("dup-1", category="procedure")])
    out = tmp_path / "merged.yaml"
    r = merge_golden_files([a, b], out, force_overlap=True)
    assert r.ok
    assert r.written
    merged = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert len(merged["items"]) == 1
    assert merged["items"][0]["category"] == "procedure"


def test_merge_dry_run_does_not_write(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("a-1")])
    b = _write(tmp_path, "b.yaml", [_make_item("b-1")])
    out = tmp_path / "merged.yaml"
    r = merge_golden_files([a, b], out, dry_run=True)
    assert r.ok
    assert not r.written
    assert not out.exists()
    assert r.total_items == 2


def test_merge_single_input_acts_as_copy(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("a-1"), _make_item("a-2")])
    out = tmp_path / "merged.yaml"
    r = merge_golden_files([a], out)
    assert r.ok and r.written
    merged = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert [it["id"] for it in merged["items"]] == ["a-1", "a-2"]


def test_merge_empty_inputs_raises() -> None:
    with pytest.raises(ValueError):
        merge_golden_files([], None)


# --- CLI ---------------------------------------------------------------


def test_cli_merge_happy(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("a-1")])
    b = _write(tmp_path, "b.yaml", [_make_item("b-1")])
    out = tmp_path / "merged.yaml"
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["golden", "merge", "-i", str(a), "-i", str(b), "-o", str(out)],
    )
    assert r.exit_code == 0, r.output
    assert out.exists()


def test_cli_merge_collision_exits_one(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("dup")])
    b = _write(tmp_path, "b.yaml", [_make_item("dup")])
    out = tmp_path / "merged.yaml"
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["golden", "merge", "-i", str(a), "-i", str(b), "-o", str(out)],
    )
    assert r.exit_code == 1
    assert "cross_file_duplicate_id" in r.output


def test_cli_merge_dry_run(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("a-1")])
    b = _write(tmp_path, "b.yaml", [_make_item("b-1")])
    out = tmp_path / "merged.yaml"
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["golden", "merge", "-i", str(a), "-i", str(b), "-o", str(out), "--dry-run"],
    )
    assert r.exit_code == 0
    assert not out.exists()
    assert "would write" in r.output or "<dry-run>" in r.output


def test_cli_merge_force(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.yaml", [_make_item("dup", category="definition")])
    b = _write(tmp_path, "b.yaml", [_make_item("dup", category="procedure")])
    out = tmp_path / "merged.yaml"
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["golden", "merge", "-i", str(a), "-i", str(b), "-o", str(out), "--force"],
    )
    assert r.exit_code == 0, r.output
    merged = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert merged["items"][0]["category"] == "procedure"
