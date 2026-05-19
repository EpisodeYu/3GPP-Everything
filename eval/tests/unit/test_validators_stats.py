"""M7.0 单测：`eval.validators.stats` 分布统计。

覆盖：
- 7 类 category 全 0 → 全 GAP
- 容差 ±tolerance 边界（target=30, actual=33 = OK；actual=36 = OVER）
- source 硬要求 hand_crafted ≥ 20；teleqna_transformed 算 INFO 不阻塞
- 未知 category → unknown_categories + ok=False
- v1.yaml 真文件 baseline 数字（不依赖具体题数，但断言关键缺口）
- CLI 退出码 0/1
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from eval.cli import app
from eval.validators.stats import (
    CATEGORY_TARGETS,
    SOURCE_TARGETS,
    compute_stats,
    format_stats,
)

pytestmark = pytest.mark.unit


def _yaml(items: list[dict]) -> str:
    lines = ["version: 1", "items:"]
    for it in items:
        lines.append(f"  - id: {it['id']}")
        for k, v in it.items():
            if k == "id":
                continue
            if isinstance(v, list):
                lines.append(f"    {k}: {v}")
            elif isinstance(v, bool):
                lines.append(f"    {k}: {str(v).lower()}")
            else:
                lines.append(f"    {k}: {v}")
    return "\n".join(lines) + "\n"


def _full_item(
    iid: str,
    category: str,
    source: str = "hand_crafted",
    language: str = "en",
) -> dict:
    return {
        "id": iid,
        "category": category,
        "language": language,
        "source": source,
        "question": f"q {iid}?",
        "expected_specs": [],
        "expected_facts": [],
        "forbidden": [],
        "must_say_not_found": False,
    }


def test_empty_file_all_gap(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text(_yaml([]), encoding="utf-8")
    s = compute_stats(p)
    assert s.total == 0
    assert not s.ok
    for r in s.categories:
        assert r.actual == 0
        assert r.status == "GAP"
    # hand_crafted 0 < 20 → GAP
    hc = next(r for r in s.sources if r.source == "hand_crafted")
    assert hc.status == "GAP"


def test_tolerance_boundary(tmp_path: Path) -> None:
    # definition target 30；33 题 → 在容差内 OK；36 题 → OVER
    items_ok = [_full_item(f"d-{i}", "definition") for i in range(33)]
    items_over = items_ok + [_full_item(f"d-{i}", "definition") for i in range(33, 36)]

    p_ok = tmp_path / "ok.yaml"
    p_ok.write_text(_yaml(items_ok), encoding="utf-8")
    s = compute_stats(p_ok, tolerance=5)
    defn = next(r for r in s.categories if r.category == "definition")
    assert defn.status == "OK"
    assert defn.delta == 3

    p_over = tmp_path / "over.yaml"
    p_over.write_text(_yaml(items_over), encoding="utf-8")
    s2 = compute_stats(p_over, tolerance=5)
    defn2 = next(r for r in s2.categories if r.category == "definition")
    assert defn2.status == "OVER"
    assert defn2.delta == 6


def test_unknown_category_marks_not_ok(tmp_path: Path) -> None:
    items = [_full_item("x-1", "definition"), _full_item("x-2", "mystery_cat")]
    p = tmp_path / "u.yaml"
    p.write_text(_yaml(items), encoding="utf-8")
    s = compute_stats(p)
    assert s.unknown_categories == ["mystery_cat"]
    assert not s.ok


def test_source_targets_hand_crafted_gap_until_20(tmp_path: Path) -> None:
    # 全 teleqna_transformed → hand_crafted=0 → GAP
    items = [_full_item(f"t-{i}", "definition", source="teleqna_transformed") for i in range(30)]
    p = tmp_path / "t.yaml"
    p.write_text(_yaml(items), encoding="utf-8")
    s = compute_stats(p)
    hc = next(r for r in s.sources if r.source == "hand_crafted")
    assert hc.actual == 0 and hc.status == "GAP"
    tql = next(r for r in s.sources if r.source == "teleqna_transformed")
    assert tql.actual == 30 and tql.status == "INFO"


def test_hand_crafted_target_reached(tmp_path: Path) -> None:
    # 25 hand_crafted definition → hand_crafted=25 ≥ 20 OK；definition 25 vs 30 OK；其他全 GAP
    items = [_full_item(f"h-{i}", "definition") for i in range(25)]
    p = tmp_path / "h.yaml"
    p.write_text(_yaml(items), encoding="utf-8")
    s = compute_stats(p)
    hc = next(r for r in s.sources if r.source == "hand_crafted")
    assert hc.status == "OK"


def test_full_distribution_meets_targets(tmp_path: Path) -> None:
    """凑满 §3.4 目标分布（hand_crafted ≥ 20）→ ok=True。"""
    items: list[dict] = []
    for cat, target in CATEGORY_TARGETS.items():
        for i in range(target):
            items.append(_full_item(f"{cat[:3]}-{i}", cat))
    p = tmp_path / "full.yaml"
    p.write_text(_yaml(items), encoding="utf-8")
    s = compute_stats(p)
    # hand_crafted 默认 → 总 >= SOURCE_TARGETS['hand_crafted']
    assert sum(CATEGORY_TARGETS.values()) >= SOURCE_TARGETS["hand_crafted"]
    assert s.ok, format_stats(s)


def test_languages_counted(tmp_path: Path) -> None:
    items = [
        _full_item("a", "definition", language="en"),
        _full_item("b", "definition", language="zh"),
        _full_item("c", "definition", language="zh"),
    ]
    p = tmp_path / "lang.yaml"
    p.write_text(_yaml(items), encoding="utf-8")
    s = compute_stats(p)
    assert s.languages == {"en": 1, "zh": 2}


def test_v1_yaml_baseline_numbers() -> None:
    """v1.yaml 当前是 119 题 teleqna 转化（无 hand_crafted）。

    断言：hand_crafted GAP（=0 < 20）+ unknown_categories 为空 + total >= 100。
    """
    v1 = Path(__file__).resolve().parents[3] / "eval" / "golden" / "v1.yaml"
    if not v1.exists():
        pytest.skip("v1.yaml 不存在")
    s = compute_stats(v1)
    assert s.total >= 100
    hc = next(r for r in s.sources if r.source == "hand_crafted")
    assert hc.status == "GAP"
    assert not s.unknown_categories
    # 模板里写的"当前缺口"分布应与此处吻合（formula ≤ 2 / multi_section ≤ 5 / negative ≤ 5）
    by_cat = {r.category: r.actual for r in s.categories}
    assert by_cat["formula"] <= 2  # 模板写 1，留 ±1 容错
    assert "tool" not in by_cat  # 2026-05-19 砍掉
    assert by_cat["negative"] <= 5  # 模板写 3


# --- CLI ---------------------------------------------------------------


def test_cli_stats_ok_exit_zero(tmp_path: Path) -> None:
    items: list[dict] = []
    for cat, target in CATEGORY_TARGETS.items():
        for i in range(target):
            items.append(_full_item(f"{cat[:3]}-{i}", cat))
    p = tmp_path / "full.yaml"
    p.write_text(_yaml(items), encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(app, ["golden", "stats", "-f", str(p)])
    assert r.exit_code == 0, r.output
    assert "Overall: OK" in r.output


def test_cli_stats_gap_exits_one(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text(_yaml([]), encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(app, ["golden", "stats", "-f", str(p)])
    assert r.exit_code == 1
    assert "GAP" in r.output


def test_cli_stats_json(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text(_yaml([]), encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(app, ["golden", "stats", "-f", str(p), "--json"])
    assert r.exit_code == 1
    import json as _json

    body = _json.loads(r.output)
    assert body["ok"] is False
    assert body["total"] == 0
