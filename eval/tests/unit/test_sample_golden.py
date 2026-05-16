"""eval/scripts/m3_sample_golden.py 单测。

覆盖：
- 小类全收
- 大类按 spec round-robin 限量
- random.seed 确定性（同 seed → 同样本集）
- 输出 YAML schema 与 §3.5 一致
"""

from __future__ import annotations

from pathlib import Path

import yaml

from eval.scripts.m3_sample_golden import (
    DEFAULT_SEED,
    report_distribution,
    stratified_sample,
    write_v1_yaml,
)


def _mk_item(*, category: str, spec_id: str, origin: str, question: str = "Q?") -> dict:
    return {
        "category": category,
        "language": "en",
        "question": question,
        "expected_specs": [{"spec_id": spec_id, "sections": []}],
        "expected_facts": ["a", "b", "c"],
        "forbidden": [],
        "source": "teleqna_transformed",
        "teleqna_origin_id": origin,
    }


class TestStratifiedSample:
    def test_small_categories_fully_picked(self) -> None:
        items = [
            _mk_item(category="negative", spec_id="38.331", origin=f"q{i}") for i in range(3)
        ] + [
            _mk_item(category="multi_section", spec_id="23.501", origin=f"q{10+i}")
            for i in range(3)
        ]
        sampled = stratified_sample(
            items, category_targets={"negative": 10, "multi_section": 10, "definition": 60}
        )
        cats = [it["category"] for it in sampled]
        assert cats.count("negative") == 3
        assert cats.count("multi_section") == 3

    def test_definition_limited_to_target(self) -> None:
        # 100 个 definition 题，分布在 5 个 spec
        items = []
        for i in range(100):
            spec = ["38.331", "23.501", "23.502", "24.501", "38.300"][i % 5]
            items.append(_mk_item(category="definition", spec_id=spec, origin=f"q{i}"))
        sampled = stratified_sample(items, category_targets={"definition": 20}, seed=DEFAULT_SEED)
        assert len(sampled) == 20
        # spec 分布应均匀 (5 spec × 20 题 = 4/spec)
        spec_counts: dict[str, int] = {}
        for it in sampled:
            sid = it["expected_specs"][0]["spec_id"]
            spec_counts[sid] = spec_counts.get(sid, 0) + 1
        # round-robin 应该让每个 spec 拿到接近相等数量
        assert min(spec_counts.values()) >= 3
        assert max(spec_counts.values()) <= 5

    def test_seed_determinism(self) -> None:
        items = [
            _mk_item(
                category="procedure",
                spec_id="23.502" if i % 2 == 0 else "24.501",
                origin=f"q{i}",
            )
            for i in range(50)
        ]
        s1 = stratified_sample(items, category_targets={"procedure": 10}, seed=42)
        s2 = stratified_sample(items, category_targets={"procedure": 10}, seed=42)
        s3 = stratified_sample(items, category_targets={"procedure": 10}, seed=99)
        ids1 = sorted(it["teleqna_origin_id"] for it in s1)
        ids2 = sorted(it["teleqna_origin_id"] for it in s2)
        ids3 = sorted(it["teleqna_origin_id"] for it in s3)
        assert ids1 == ids2  # 同 seed 决定性
        assert ids1 != ids3  # 不同 seed 出不同样本

    def test_target_larger_than_available(self) -> None:
        items = [
            _mk_item(category="definition", spec_id="38.331", origin=f"q{i}") for i in range(5)
        ]
        # target 100 > available 5 → 全收 5
        sampled = stratified_sample(items, category_targets={"definition": 100})
        assert len(sampled) == 5


class TestWriteV1Yaml:
    def test_writes_yaml_with_ids_and_metadata(self, tmp_path: Path) -> None:
        items = [
            _mk_item(category="definition", spec_id="38.331", origin="q1"),
            _mk_item(category="definition", spec_id="23.501", origin="q2"),
            _mk_item(category="procedure", spec_id="23.502", origin="q3"),
            _mk_item(category="negative", spec_id="38.331", origin="q4"),
        ]
        out = tmp_path / "v1.yaml"
        write_v1_yaml(items, out_path=out)
        doc = yaml.safe_load(out.read_text())
        assert doc["version"] == 1
        assert doc["total"] == 4
        assert sorted(doc["categories"]) == ["definition", "negative", "procedure"]
        assert doc["sources"] == ["teleqna_transformed"]
        ids = [it["id"] for it in doc["items"]]
        # 每 category 各自连续编号
        assert "def-001" in ids
        assert "def-002" in ids
        assert "proc-001" in ids
        assert "neg-001" in ids


class TestReportDistribution:
    def test_basic(self) -> None:
        items = [
            _mk_item(category="definition", spec_id="38.331", origin="q1"),
            _mk_item(category="definition", spec_id="38.331", origin="q2"),
            _mk_item(category="procedure", spec_id="23.501", origin="q3"),
        ]
        report = report_distribution(items)
        assert report["total"] == 3
        assert report["by_category"] == {"definition": 2, "procedure": 1}
        assert report["by_spec"] == {"23.501": 1, "38.331": 2}
