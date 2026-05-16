"""eval/retrieval/metrics.py 单测（纯函数，无外部依赖）。"""

from __future__ import annotations

import pytest

from eval.retrieval.metrics import (
    ExpectedSpec,
    HitRef,
    compute_metrics,
    is_section_hit,
    is_section_prefix,
    is_spec_hit,
    per_question_metrics,
)


class TestSectionPrefix:
    @pytest.mark.parametrize(
        "expected,hit_path,want",
        [
            ("4.2", ["4", "2"], True),
            ("4.2", ["4", "2", "2", "3"], True),
            ("4.2.2", ["4", "2", "2", "3"], True),
            ("4.2.2.3", ["4", "2", "2", "3"], True),
            ("4.2.2.4", ["4", "2", "2", "3"], False),
            ("4.20", ["4", "2"], False),
            ("4.20", ["4", "20"], True),
            ("3", ["4", "2"], False),
            ("4.2", [], False),
            ("", ["4", "2"], False),
        ],
    )
    def test_section_prefix(self, expected: str, hit_path: list[str], want: bool) -> None:
        assert is_section_prefix(expected, hit_path) is want

    def test_string_hit_path(self) -> None:
        # 兼容传入字符串（应自动 split by "."）
        assert is_section_prefix("4.2", "4.2.2.3") is True
        assert is_section_prefix("4.2", "4.20") is False


class TestSectionHit:
    def test_spec_mismatch(self) -> None:
        e = ExpectedSpec(spec_id="38.331", sections=("4.2",))
        h = HitRef(spec_id="23.501", section_path=("4", "2"))
        assert is_section_hit(e, h) is False

    def test_no_sections_means_whole_spec(self) -> None:
        e = ExpectedSpec(spec_id="38.331")
        h = HitRef(spec_id="38.331", section_path=("99", "1"))
        assert is_section_hit(e, h) is True

    def test_match_via_section_path(self) -> None:
        e = ExpectedSpec(spec_id="38.331", sections=("5.3.5",))
        h = HitRef(spec_id="38.331", section_path=("5", "3", "5", "1"))
        assert is_section_hit(e, h) is True

    def test_match_via_clause_when_no_section_path(self) -> None:
        e = ExpectedSpec(spec_id="38.331", sections=("5.3.5",))
        h = HitRef(spec_id="38.331", section_path=(), clause="5.3.5.1")
        assert is_section_hit(e, h) is True

    def test_multiple_expected_sections_any_hits(self) -> None:
        e = ExpectedSpec(spec_id="23.502", sections=("4.2.2", "4.3.4"))
        h_match2 = HitRef(spec_id="23.502", section_path=("4", "3", "4", "1"))
        h_no = HitRef(spec_id="23.502", section_path=("4", "9"))
        assert is_section_hit(e, h_match2) is True
        assert is_section_hit(e, h_no) is False


class TestSpecHit:
    def test_basic(self) -> None:
        es = [ExpectedSpec("38.331"), ExpectedSpec("23.501")]
        assert is_spec_hit(es, HitRef("38.331")) is True
        assert is_spec_hit(es, HitRef("23.501")) is True
        assert is_spec_hit(es, HitRef("24.501")) is False
        assert is_spec_hit([], HitRef("38.331")) is False


class TestPerQuestionMetrics:
    def test_perfect_top1(self) -> None:
        es = [ExpectedSpec("38.331", ("5.3.5",))]
        hits = [HitRef("38.331", ("5", "3", "5", "1"))]
        m = per_question_metrics(es, hits, k_list=(5, 10))
        assert m["spec_recall@5"] == 1.0
        assert m["section_recall@5"] == 1.0
        assert m["precision@5"] == pytest.approx(0.2)  # 1/5
        assert m["mrr"] == 1.0
        assert m["mrr_spec"] == 1.0

    def test_no_hits_at_all(self) -> None:
        es = [ExpectedSpec("38.331", ("5.3.5",))]
        hits = [HitRef("23.501", ("4", "2"))] * 5
        m = per_question_metrics(es, hits, k_list=(5, 10))
        assert m["spec_recall@5"] == 0.0
        assert m["section_recall@5"] == 0.0
        assert m["mrr"] == 0.0
        assert m["mrr_spec"] == 0.0

    def test_section_match_at_rank3_of_5(self) -> None:
        es = [ExpectedSpec("38.331", ("5.3.5",))]
        hits = [
            HitRef("38.331", ("4", "2")),  # spec 命中 / section 不命中
            HitRef("38.331", ("4", "3")),
            HitRef("38.331", ("5", "3", "5", "2")),  # ← section 命中 (rank=3)
            HitRef("38.331", ("9", "9")),
            HitRef("23.501", ("4",)),
        ]
        m = per_question_metrics(es, hits, k_list=(5, 10))
        assert m["spec_recall@5"] == 1.0
        assert m["section_recall@5"] == 1.0
        assert m["precision@5"] == pytest.approx(1 / 5)
        assert m["mrr"] == pytest.approx(1 / 3)
        assert m["mrr_spec"] == 1.0  # 第一个 spec 命中在 rank=1

    def test_empty_hits(self) -> None:
        es = [ExpectedSpec("38.331")]
        m = per_question_metrics(es, [], k_list=(5,))
        assert m == {
            "hits_total": 0.0,
            "mrr": 0.0,
            "mrr_spec": 0.0,
            "spec_recall@5": 0.0,
            "section_recall@5": 0.0,
            "precision@5": 0.0,
        }


class TestComputeMetrics:
    def test_aggregate(self) -> None:
        rows = [
            {
                "hits_total": 10.0,
                "mrr": 1.0,
                "mrr_spec": 1.0,
                "spec_recall@5": 1.0,
                "section_recall@5": 1.0,
                "precision@5": 0.2,
                "spec_recall@10": 1.0,
                "section_recall@10": 1.0,
                "precision@10": 0.1,
            },
            {
                "hits_total": 10.0,
                "mrr": 0.0,
                "mrr_spec": 0.5,
                "spec_recall@5": 1.0,
                "section_recall@5": 0.0,
                "precision@5": 0.0,
                "spec_recall@10": 1.0,
                "section_recall@10": 0.0,
                "precision@10": 0.0,
            },
        ]
        agg = compute_metrics(rows, k_list=(5, 10))
        assert agg.n_questions == 2
        assert agg.spec_recall_at[5] == 1.0
        assert agg.section_recall_at[5] == pytest.approx(0.5)
        assert agg.mrr == pytest.approx(0.5)
        assert agg.mrr_spec == pytest.approx(0.75)
        d = agg.to_dict()
        assert d["spec_recall_at"]["@5"] == 1.0

    def test_empty(self) -> None:
        agg = compute_metrics([], k_list=(5,))
        assert agg.n_questions == 0
        assert agg.spec_recall_at == {}
