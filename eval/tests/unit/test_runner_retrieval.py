"""eval/runner_retrieval.py 单测（不调真实 Qdrant / LiteLLM）。

覆盖：
- load_golden：YAML → GoldenItem，含 expected_specs / sections / 各 optional 字段
- decide_winner：3 规则（R@10 差距大 / MRR 差距大 / tie 回退 1024）
- evaluate_retrieval：fake retriever → per-question metrics + 聚合
- write_report_markdown：JSON / MD 输出存在 + 关键字段在
- _percentile：基本正确
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from eval.retrieval.metrics import RetrievalMetrics
from eval.retrieval.retriever import Hit, Retriever
from eval.runner_retrieval import (
    DECISION_THRESHOLD,
    DimResult,
    GoldenItem,
    _percentile,
    decide_winner,
    evaluate_retrieval,
    load_golden,
    write_report_markdown,
)

# ----------------- load_golden -----------------


class TestLoadGolden:
    def test_basic(self, tmp_path: Path) -> None:
        doc = {
            "version": 1,
            "created_at": "2026-05-16",
            "total": 2,
            "items": [
                {
                    "id": "def-001",
                    "category": "definition",
                    "language": "en",
                    "question": "What is PDU Session?",
                    "expected_specs": [{"spec_id": "23.501", "sections": ["3.1", "5.6"]}],
                    "expected_facts": ["association", "UE", "DN"],
                    "forbidden": ["4G"],
                    "source": "teleqna_transformed",
                    "teleqna_origin_id": "question 12",
                    "notes": "5G core concept",
                },
                {
                    "id": "neg-001",
                    "category": "negative",
                    "language": "en",
                    "question": "What is MAC address of UE PDU Session?",
                    "expected_specs": [],
                    "expected_facts": [],
                    "must_say_not_found": True,
                    "source": "hand_crafted",
                },
            ],
        }
        p = tmp_path / "v1.yaml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        items = load_golden(p)
        assert len(items) == 2
        assert items[0].id == "def-001"
        assert items[0].expected_specs[0].spec_id == "23.501"
        assert items[0].expected_specs[0].sections == ("3.1", "5.6")
        assert items[0].expected_facts == ["association", "UE", "DN"]
        assert items[0].teleqna_origin_id == "question 12"
        assert items[1].must_say_not_found is True
        assert items[1].expected_specs == []

    def test_missing_items_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "v1.yaml"
        p.write_text(yaml.safe_dump({"version": 1}), encoding="utf-8")
        with pytest.raises(ValueError, match="missing 'items'"):
            load_golden(p)


# ----------------- decide_winner -----------------


def _mk_dim_result(dim: int, r10: float, mrr: float, n: int = 60) -> DimResult:
    m = RetrievalMetrics(
        n_questions=n,
        spec_recall_at={5: r10, 10: r10, 20: r10},
        section_recall_at={5: r10, 10: r10, 20: r10},
        precision_at={5: 0.0, 10: 0.0, 20: 0.0},
        mrr=mrr,
        mrr_spec=mrr,
    )
    return DimResult(dim=dim, metrics=m, latency_ms_p50=10.0, latency_ms_p95=20.0, n_questions=n)


class TestDecideWinner:
    def test_r10_gap_picks_higher(self) -> None:
        v = decide_winner(
            {
                2048: _mk_dim_result(2048, r10=0.85, mrr=0.7),
                1024: _mk_dim_result(1024, r10=0.80, mrr=0.7),
            }
        )
        assert v.winner_dim == 2048
        assert v.tie_fallback is False
        assert v.r10_diff == pytest.approx(0.05)
        assert "R@10" in v.reason

    def test_r10_gap_picks_lower_dim_winner(self) -> None:
        v = decide_winner(
            {
                2048: _mk_dim_result(2048, r10=0.80, mrr=0.7),
                1024: _mk_dim_result(1024, r10=0.85, mrr=0.7),
            }
        )
        assert v.winner_dim == 1024
        assert v.r10_diff == pytest.approx(-0.05)

    def test_r10_tie_then_mrr_decides(self) -> None:
        # R@10 差距 0.01 < 0.02 → 看 MRR；MRR 2048 > 1024 + 0.03 → 2048 赢
        v = decide_winner(
            {
                2048: _mk_dim_result(2048, r10=0.81, mrr=0.75),
                1024: _mk_dim_result(1024, r10=0.80, mrr=0.72),
            }
        )
        assert v.winner_dim == 2048
        assert v.tie_fallback is False
        assert "MRR" in v.reason

    def test_full_tie_falls_back_to_1024(self) -> None:
        v = decide_winner(
            {
                2048: _mk_dim_result(2048, r10=0.81, mrr=0.75),
                1024: _mk_dim_result(1024, r10=0.80, mrr=0.745),
            }
        )
        # R@10 差 0.01 / MRR 差 0.005 — 都 <= 0.02
        assert v.winner_dim == 1024
        assert v.tie_fallback is True
        assert "回退" in v.reason

    def test_invalid_dims_raises(self) -> None:
        with pytest.raises(ValueError, match="expected dims"):
            decide_winner({2048: _mk_dim_result(2048, 0.8, 0.7)})

    def test_threshold_constant(self) -> None:
        assert DECISION_THRESHOLD == 0.02


# ----------------- evaluate_retrieval (fake retriever) -----------------


def _hit(spec_id: str, section: tuple[str, ...] = (), score: float = 0.9) -> Hit:
    return Hit(
        chunk_id=f"{spec_id}-{'.'.join(section) or 'x'}",
        score=score,
        spec_id=spec_id,
        clause=".".join(section),
        section_path=list(section),
        section_title=".".join(section),
        chunk_type="text",
        content="...",
    )


class _FakeRetriever(Retriever):
    """绕过真实 embed + qdrant；按 query → 预定义 hits_by_dim 返回。"""

    def __init__(self, fixed: dict[str, dict[int, list[Hit]]]) -> None:
        # 不调 super().__init__ 避免起 client
        self._fixed = fixed
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def search_multidim(  # type: ignore[override]
        self,
        query: str,
        *,
        dims: Sequence[int] = (2048, 1024),
        top_k: int = 20,
        spec_filter: Sequence[str] | None = None,
    ) -> dict[int, list[Hit]]:
        self.calls.append((query, tuple(dims)))
        return self._fixed.get(query, {d: [] for d in dims})

    def close(self) -> None:
        pass


class TestEvaluateRetrieval:
    def test_basic_aggregation(self, tmp_path: Path) -> None:
        golden = [
            GoldenItem(
                id="def-001",
                category="definition",
                language="en",
                question="Q1",
                expected_specs=[],  # 测试 helper 直接构造，下面 patch
            ),
        ]
        from eval.retrieval.metrics import ExpectedSpec

        golden[0].expected_specs = [ExpectedSpec("38.331", ("5.3.5",))]

        fixed = {
            "Q1": {
                2048: [_hit("38.331", ("5", "3", "5", "1"), 0.95)],  # section 命中
                1024: [_hit("23.501", ("3", "1"), 0.85)],  # 完全没中
            }
        }
        r = _FakeRetriever(fixed)
        by_dim, rows = evaluate_retrieval(golden, retriever=r, top_k=5, k_list=(5, 10))

        assert set(by_dim.keys()) == {2048, 1024}
        assert by_dim[2048].n_questions == 1
        assert by_dim[2048].metrics.section_recall_at[5] == 1.0
        assert by_dim[1024].metrics.section_recall_at[5] == 0.0
        assert len(rows) == 1
        assert rows[0].metrics_by_dim[2048]["section_recall@5"] == 1.0
        assert rows[0].metrics_by_dim[1024]["section_recall@5"] == 0.0

    def test_empty_question_skipped(self) -> None:
        from eval.retrieval.metrics import ExpectedSpec

        golden = [
            GoldenItem(
                id="empty",
                category="definition",
                language="en",
                question="",
                expected_specs=[ExpectedSpec("38.331")],
            )
        ]
        r = _FakeRetriever({})
        by_dim, rows = evaluate_retrieval(golden, retriever=r)
        assert rows == []
        assert by_dim[2048].n_questions == 0


# ----------------- write_report_markdown -----------------


class TestWriteReportMarkdown:
    def test_writes_md_and_json(self, tmp_path: Path) -> None:
        by_dim = {
            2048: _mk_dim_result(2048, 0.85, 0.70),
            1024: _mk_dim_result(1024, 0.83, 0.69),
        }
        verdict = decide_winner(by_dim)
        rows = [
            # 简化：直接构造少量行
            type(
                "R",
                (),
                {  # type: ignore[call-arg]
                    "item_id": "def-001",
                    "category": "definition",
                    "expected_specs": ["23.501"],
                    "metrics_by_dim": {
                        2048: {"section_recall@10": 1.0},
                        1024: {"section_recall@10": 0.0},
                    },
                    "latency_ms_by_dim": {2048: 12.0, 1024: 9.0},
                },
            )()
        ]
        out_dir = tmp_path / "out"
        golden_path = tmp_path / "v1.yaml"
        golden_path.write_text("items: []", encoding="utf-8")

        md = write_report_markdown(
            out_dir=out_dir,
            by_dim=by_dim,
            verdict=verdict,
            rows=rows,
            golden_path=golden_path,
        )
        assert md.exists()
        md_text = md.read_text()
        assert "M3 维度决胜" in md_text
        assert "winner = " in md_text
        assert str(verdict.winner_dim) in md_text
        json_path = out_dir / "results.json"
        assert json_path.exists()


# ----------------- _percentile -----------------


class TestPercentile:
    def test_p50_p95(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(xs, 0.5) == 3.0
        assert _percentile(xs, 1.0) == 5.0
        assert _percentile(xs, 0.0) == 1.0

    def test_empty(self) -> None:
        assert _percentile([], 0.5) == 0.0
