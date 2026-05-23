"""单测 `scripts/dev/retrieval_ablation.py` 的纯函数 metric 与配置 label。

ablation 脚本本身是 dev 工具（非生产路径），但 metric 函数与生产
`retrieve_node` 召回口径需要严格对齐 `docs/03-development/06-...md §3.5`：

- spec_recall@K = top-K 中至少一条命中 expected.spec_id
- section_recall@K = top-K 中至少一条 (spec_id, section_path) 匹配 expected
  section（按 segment 前缀语义，`"4.2"` 命中 `"4.2.2.3"` 但不命中 `"4.20"`）
- MRR = 第一个命中所在 1/rank
- negative item（expected_specs=[]）→ NaN 不进聚合

口径与 `eval/retrieval/metrics.py` 同步；本测试是两边漂移的哨兵
（如果哪天 eval 那侧改了语义这里要同步）。
"""

from __future__ import annotations

import math

import pytest

from app.retrieval.models import RetrievedChunk
from scripts.dev.retrieval_ablation import (
    AblationConfig,
    is_section_hit,
    is_section_prefix,
    is_spec_hit,
    load_golden,
    per_question_metrics,
)


def _chunk(spec_id: str, section: str = "", chunk_id: str = "c") -> RetrievedChunk:
    sp = tuple(s for s in section.split(".") if s) if section else ()
    return RetrievedChunk(
        chunk_id=chunk_id,
        spec_id=spec_id,
        section_path=sp,
        section_title="",
        chunk_type="text",
        content="x",
    )


class TestIsSectionPrefix:
    def test_exact_match(self) -> None:
        assert is_section_prefix("4.2", ["4", "2"]) is True

    def test_proper_prefix(self) -> None:
        assert is_section_prefix("4.2", ["4", "2", "2", "3"]) is True

    def test_segment_boundary_does_not_match_lexical(self) -> None:
        # "4.20" 不应被 ["4","2"] 命中（M7.5 哨兵：段级前缀语义）
        assert is_section_prefix("4.20", ["4", "2"]) is False

    def test_empty_expected_returns_false(self) -> None:
        assert is_section_prefix("", ["4", "2"]) is False

    def test_expected_longer_than_hit(self) -> None:
        assert is_section_prefix("4.2.2.3", ["4", "2"]) is False


class TestIsSectionHit:
    def test_spec_mismatch(self) -> None:
        assert is_section_hit(("23.501", ("5.2.1",)), _chunk("38.331", "5.2.1")) is False

    def test_empty_sections_means_spec_only(self) -> None:
        # expected.sections=() → 只要 spec_id 命中即算
        assert is_section_hit(("23.501", ()), _chunk("23.501", "9.9.9")) is True

    def test_one_of_sections_matches(self) -> None:
        assert is_section_hit(("23.501", ("5.2.1", "6.3")), _chunk("23.501", "6.3.5")) is True


class TestPerQuestionMetrics:
    def test_first_position_hit(self) -> None:
        expected = [("23.501", ("5.2.1",))]
        hits = [_chunk("23.501", "5.2.1"), _chunk("38.331", "1.1")]
        m = per_question_metrics(expected, hits, k_list=(5, 10))
        assert m["mrr"] == 1.0
        assert m["mrr_spec"] == 1.0
        assert m["section_recall@5"] == 1.0
        assert m["spec_recall@5"] == 1.0

    def test_section_miss_but_spec_hit(self) -> None:
        # spec 同，section 不同 → spec hit, section miss
        expected = [("23.501", ("5.2.1",))]
        hits = [_chunk("38.331", "1"), _chunk("23.501", "9.9")]
        m = per_question_metrics(expected, hits, k_list=(5,))
        assert m["spec_recall@5"] == 1.0
        assert m["section_recall@5"] == 0.0
        assert m["mrr"] == 0.0  # section MRR
        assert m["mrr_spec"] == 0.5  # spec 在 rank 2

    def test_no_hits(self) -> None:
        expected = [("23.501", ("5.2.1",))]
        m = per_question_metrics(expected, [], k_list=(5, 10))
        assert m["section_recall@5"] == 0.0
        assert m["spec_recall@5"] == 0.0
        assert m["mrr"] == 0.0

    def test_negative_item_returns_nan(self) -> None:
        # negative item: expected_specs=[] → NaN（聚合时 _safe_mean 跳过）
        m = per_question_metrics([], [_chunk("23.501")], k_list=(5,))
        assert math.isnan(m["section_recall@5"])
        assert math.isnan(m["mrr"])

    def test_first_section_hit_at_rank_3_gives_mrr_one_third(self) -> None:
        expected = [("23.501", ("5.2.1",))]
        hits = [
            _chunk("38.331", "1"),
            _chunk("38.331", "2"),
            _chunk("23.501", "5.2.1"),
        ]
        m = per_question_metrics(expected, hits, k_list=(5,))
        assert m["mrr"] == pytest.approx(1.0 / 3.0)


class TestIsSpecHit:
    def test_hit(self) -> None:
        assert is_spec_hit([("23.501", ("5.2.1",))], _chunk("23.501")) is True

    def test_miss(self) -> None:
        assert is_spec_hit([("23.501", ("5.2.1",))], _chunk("38.331")) is False


class TestAblationConfigLabel:
    def test_label_format_with_rerank(self) -> None:
        cfg = AblationConfig(
            name="test", dense_top_k=30, sparse_top_k=30, rrf_k=60, final_top_n=50, rerank_top_k=5
        )
        assert cfg.label == "d30/s30/rrf60/top50/rerank5"

    def test_label_format_without_rerank(self) -> None:
        cfg = AblationConfig(
            name="test2",
            dense_top_k=50,
            sparse_top_k=20,
            rrf_k=30,
            final_top_n=40,
            rerank_top_k=None,
        )
        assert cfg.label == "d50/s20/rrf30/top40/no-rerank"

    def test_label_includes_query_prefix(self) -> None:
        cfg = AblationConfig(
            name="t3",
            dense_top_k=50,
            sparse_top_k=50,
            rrf_k=60,
            final_top_n=80,
            rerank_top_k=5,
            query_prefix="[NR] ",
        )
        assert "prefix=" in cfg.label


class TestMergedChunkSemantics:
    """Chunker 把 §4.4.4.3 / §4.4.4.4 / §4.4.4.5 合并到 §4.4.4 时，chunk 的
    section_title 形如 ``<merged: 4.4.4.3 / 4.4.4.4 / 4.4.4.5>``，且 chunk 内容
    实际包含这些子节正文。原 metric 仅看 hit_path 前缀会把这种 chunk 命中 expected
    子节误判为 miss；此哨兵守住 M7.6 修正后的等价覆盖语义。"""

    def _merged_chunk(self, spec_id: str, clause: str, merged: str) -> RetrievedChunk:
        sp = tuple(s for s in clause.split(".") if s) if clause else ()
        return RetrievedChunk(
            chunk_id="merged",
            spec_id=spec_id,
            section_path=sp,
            section_title=f"<merged: {merged}>",
            chunk_type="text",
            content="x",
        )

    def test_merged_child_clause_is_hit(self) -> None:
        c = self._merged_chunk("38.211", "4.4.4", "4.4.4.3 / 4.4.4.4 / 4.4.4.5")
        assert is_section_hit(("38.211", ("4.4.4.3",)), c) is True
        assert is_section_hit(("38.211", ("4.4.4.5",)), c) is True

    def test_clause_outside_merged_set_is_miss(self) -> None:
        c = self._merged_chunk("38.211", "4.4.4", "4.4.4.3 / 4.4.4.4")
        # 4.4.4.1 不在 merged 列表里 → 不算 hit
        assert is_section_hit(("38.211", ("4.4.4.1",)), c) is False

    def test_merged_with_top_level_subsections(self) -> None:
        # 38.211 §4 chunk title=<merged: 4.1 / 4.2> → expected '4.2' 应命中
        c = self._merged_chunk("38.211", "4", "4.1 / 4.2")
        assert is_section_hit(("38.211", ("4.2",)), c) is True
        assert is_section_hit(("38.211", ("4.3",)), c) is False

    def test_merged_wrong_spec_still_miss(self) -> None:
        c = self._merged_chunk("38.211", "6.3.2.5", "6.3.2.5.1 / 6.3.2.5.2")
        assert is_section_hit(("38.212", ("6.3.2.5.1",)), c) is False


class TestLoadGolden:
    def test_load_minimal(self, tmp_path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text(
            "version: 1\nitems:\n"
            "  - id: a\n    category: definition\n    language: en\n"
            "    question: Q?\n    source: hand_crafted\n"
            "    expected_specs: [{spec_id: '23.501', sections: ['5.2.1']}]\n"
            "  - id: b\n    category: negative\n    language: zh\n"
            "    question: Q2\n    source: hand_crafted\n    must_say_not_found: true\n"
            "    expected_specs: []\n",
            encoding="utf-8",
        )
        items = load_golden(p)
        assert len(items) == 2
        assert items[0].id == "a"
        assert items[0].expected_specs == [("23.501", ("5.2.1",))]
        assert items[1].must_say_not_found is True
        assert items[1].expected_specs == []

    def test_source_filter(self, tmp_path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text(
            "version: 1\nitems:\n"
            "  - id: a\n    category: definition\n    language: en\n"
            "    source: teleqna_transformed\n    question: Q?\n"
            "    expected_specs: [{spec_id: '23.501'}]\n"
            "  - id: b\n    category: definition\n    language: zh\n"
            "    source: hand_crafted\n    question: Q?\n"
            "    expected_specs: [{spec_id: '38.331'}]\n",
            encoding="utf-8",
        )
        items = load_golden(p, source_filter="hand_crafted")
        assert len(items) == 1
        assert items[0].id == "b"


__all__: list[str] = []
