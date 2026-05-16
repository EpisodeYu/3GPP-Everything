"""m3_chunk_id_drift 脚本核心 helper 单测。

不联 Qdrant / HF；只测算法正确性 + DriftReport 序列化。
"""

from __future__ import annotations

import json

from ingestion.scripts.m3_chunk_id_drift import (
    DEFAULT_POC_17,
    DriftReport,
    SpecDrift,
    diff_chunk_ids,
)


def test_diff_chunk_ids_no_change() -> None:
    a = {"x", "y", "z"}
    b = {"x", "y", "z"}
    inter, only_a, only_b, drift = diff_chunk_ids(a, b)
    assert inter == 3
    assert only_a == 0
    assert only_b == 0
    assert drift == 0.0


def test_diff_chunk_ids_full_replacement() -> None:
    a = {"x", "y"}
    b = {"u", "v"}
    inter, only_a, only_b, drift = diff_chunk_ids(a, b)
    assert inter == 0
    assert only_a == 2
    assert only_b == 2
    # 对称差 = 4，并集 = 4 → drift = 1.0
    assert drift == 1.0


def test_diff_chunk_ids_partial_overlap() -> None:
    # 80% 命中：4 个共享，1 个旧丢，1 个新增
    a = {"a", "b", "c", "d", "e"}  # 5
    b = {"a", "b", "c", "d", "f"}  # 5；共 4 个共享
    inter, only_a, only_b, drift = diff_chunk_ids(a, b)
    assert inter == 4
    assert only_a == 1
    assert only_b == 1
    # union = 6，sym_diff = 2 → drift = 1/3
    assert abs(drift - (2 / 6)) < 1e-9


def test_diff_chunk_ids_empty_returns_zero() -> None:
    """两边都空时不应除 0。"""
    inter, only_a, only_b, drift = diff_chunk_ids(set(), set())
    assert (inter, only_a, only_b, drift) == (0, 0, 0, 0.0)


def test_diff_chunk_ids_only_old() -> None:
    """旧有新无：可能是 chunker 把内容合并/删了。"""
    a = {"a", "b"}
    b: set[str] = set()
    inter, only_a, only_b, drift = diff_chunk_ids(a, b)
    assert inter == 0
    assert only_a == 2
    assert only_b == 0
    assert drift == 1.0


def test_default_poc_17_count_and_membership() -> None:
    assert len(DEFAULT_POC_17) == 17
    # 17 篇 POC 必须含 38.331（M1）+ Task D 16 篇代表 spec
    assert "38.331" in DEFAULT_POC_17
    assert "23.501" in DEFAULT_POC_17
    assert "38.300" in DEFAULT_POC_17
    # 不应有重复
    assert len(set(DEFAULT_POC_17)) == 17


def test_drift_report_serialization_roundtrip() -> None:
    rep = DriftReport(threshold=0.05)
    rep.spec_ids = ["38.331", "38.300"]
    rep.per_spec = [
        SpecDrift(
            spec_id="38.331",
            old_count=10695,
            new_count=10695,
            intersection=10695,
            only_old=0,
            only_new=0,
            drift=0.0,
        ),
        SpecDrift(
            spec_id="38.300",
            old_count=961,
            new_count=970,
            intersection=950,
            only_old=11,
            only_new=20,
            drift=0.031,
        ),
    ]
    rep.total_old = 10695 + 961
    rep.total_new = 10695 + 970
    rep.total_intersection = 10695 + 950
    rep.total_only_old = 11
    rep.total_only_new = 20
    rep.overall_drift = (11 + 20) / (10695 + 950 + 11 + 20)
    rep.passed = rep.overall_drift <= 0.05

    j = rep.to_json()
    # 序列化后能 JSON dumps（即不含 set / dataclass）
    s = json.dumps(j, ensure_ascii=False)
    parsed = json.loads(s)
    assert parsed["threshold"] == 0.05
    assert parsed["passed"] is True
    assert parsed["totals"]["only_old"] == 11
    assert parsed["totals"]["only_new"] == 20
    assert len(parsed["per_spec"]) == 2
    assert parsed["per_spec"][1]["spec_id"] == "38.300"
    assert parsed["per_spec"][1]["drift"] == 0.031


def test_drift_report_fails_when_overall_exceeds_threshold() -> None:
    rep = DriftReport(threshold=0.05)
    rep.total_old = 100
    rep.total_new = 100
    rep.total_intersection = 90
    rep.total_only_old = 10
    rep.total_only_new = 10
    rep.overall_drift = 20 / 110  # ~18.2%
    rep.passed = rep.overall_drift <= 0.05
    assert rep.passed is False
    j = rep.to_json()
    assert j["passed"] is False
    assert j["overall_drift"] > 0.05


def test_drift_report_marks_failure_on_per_spec_error() -> None:
    """单 spec error（如 Qdrant scroll 失败）应让整体 passed = False。"""
    # 模拟 run() 末尾的判定逻辑：overall_drift OK 但 per_spec 有 error
    overall_drift = 0.0
    per_spec = [
        SpecDrift(spec_id="38.331", error="HTTPStatusError: 503"),
        SpecDrift(spec_id="38.300", drift=0.0),
    ]
    threshold = 0.05
    passed = overall_drift <= threshold and not any(s.error for s in per_spec)
    assert passed is False
