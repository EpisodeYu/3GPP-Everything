"""eval/scripts/m6_regression.py::decide_verdict 单测。

只覆盖纯函数 ``decide_verdict``。报告写盘 / live retrieval 走脚本手工触发，不在这里测。

口径见 ``eval-results/m6-retrieval-baseline.md``：
M6 全量 dense-only retrieval 与 M3 17-spec 不是同一草垛，
默认 baseline 模式不下 FAIL；strict 模式留给 M4 rerank 接上后同口径对照用。
"""

from __future__ import annotations

import pytest

from eval.scripts.m6_regression import (
    M3_BASELINE_1024,
    REGRESSION_TOLERANCE_PP,
    decide_verdict,
)


class TestDecideVerdictBaselineMode:
    """baseline 模式（默认）：任何输入都不下 FAIL。"""

    def test_far_below_m3_baseline_does_not_fail(self) -> None:
        """M6 实测 spec R@10 = 0.580（−23.5pp vs M3 0.815），baseline 模式仍不 FAIL。"""
        v = decide_verdict(
            spec_recall_at_10=0.580,
            section_recall_at_10=0.437,
            mode="baseline",
        )
        assert v.is_fail is False
        assert v.mode == "baseline"
        assert "BASELINE" in v.headline
        assert v.deltas_pp["spec_recall@10"] == pytest.approx(-23.5, abs=0.05)
        assert v.deltas_pp["section_recall@10"] == pytest.approx(-21.0, abs=0.05)

    def test_at_or_above_baseline_does_not_fail(self) -> None:
        """指标平于或高于 M3 时同样不下 FAIL（信息性输出）。"""
        v = decide_verdict(
            spec_recall_at_10=0.85,
            section_recall_at_10=0.66,
            mode="baseline",
        )
        assert v.is_fail is False
        assert "BASELINE" in v.headline
        assert v.deltas_pp["spec_recall@10"] > 0
        assert v.deltas_pp["section_recall@10"] > 0


class TestDecideVerdictStrictMode:
    """strict 模式：任一指标 < baseline - tolerance_pp 即 FAIL。"""

    def test_within_tolerance_passes(self) -> None:
        """两个指标都在 M3 baseline ±2pp 内 → PASS。"""
        v = decide_verdict(
            spec_recall_at_10=M3_BASELINE_1024["spec_recall@10"] - 0.01,  # −1pp
            section_recall_at_10=M3_BASELINE_1024["section_recall@10"] - 0.015,  # −1.5pp
            mode="strict",
        )
        assert v.is_fail is False
        assert "PASS" in v.headline
        assert v.mode == "strict"

    def test_spec_recall_below_tolerance_fails(self) -> None:
        """spec R@10 跌 −3pp（超 −2pp 容差）→ FAIL。"""
        v = decide_verdict(
            spec_recall_at_10=M3_BASELINE_1024["spec_recall@10"] - 0.03,
            section_recall_at_10=M3_BASELINE_1024["section_recall@10"],
            mode="strict",
        )
        assert v.is_fail is True
        assert "FAIL" in v.headline

    def test_section_recall_below_tolerance_fails(self) -> None:
        """section R@10 跌 −5pp → FAIL（即使 spec R@10 持平）。"""
        v = decide_verdict(
            spec_recall_at_10=M3_BASELINE_1024["spec_recall@10"],
            section_recall_at_10=M3_BASELINE_1024["section_recall@10"] - 0.05,
            mode="strict",
        )
        assert v.is_fail is True
        assert "FAIL" in v.headline

    def test_m6_full_data_under_strict_would_fail(self) -> None:
        """sanity：strict 模式跑 M6 实测数据应 FAIL —— 这正是为何默认要用 baseline 模式。"""
        v = decide_verdict(
            spec_recall_at_10=0.580,
            section_recall_at_10=0.437,
            mode="strict",
        )
        assert v.is_fail is True
        assert "FAIL" in v.headline

    def test_custom_tolerance_and_baseline(self) -> None:
        """容差与 baseline 可显式注入；用于 M4 rerank 接上后对照前次 rerank baseline。"""
        rerank_prev_baseline = {"spec_recall@10": 0.75, "section_recall@10": 0.60}
        v = decide_verdict(
            spec_recall_at_10=0.73,
            section_recall_at_10=0.58,
            mode="strict",
            baseline=rerank_prev_baseline,
            tolerance_pp=3.0,
        )
        assert v.is_fail is False  # −2pp / −2pp 在 ±3pp 内
        assert "PASS" in v.headline


def test_regression_tolerance_constant_unchanged() -> None:
    """文档与脚本对齐：默认容差 = 2.0pp（CLAUDE.md §5.6 不可降级）。"""
    assert pytest.approx(2.0) == REGRESSION_TOLERANCE_PP
