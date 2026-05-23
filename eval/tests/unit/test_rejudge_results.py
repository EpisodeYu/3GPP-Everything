"""Unit tests for eval.scripts.rejudge_results.

聚焦纯函数：substring 指标、chunk content hydrate、aggregate。
"""

from __future__ import annotations

import json

from eval.scripts import rejudge_results as M


class TestSubstring:
    def test_fact_coverage_empty(self) -> None:
        assert M._fact_coverage("any", []) is None

    def test_fact_coverage_basic(self) -> None:
        assert M._fact_coverage("UE includes PDU session.", ["PDU session", "missing"]) == 0.5

    def test_fact_coverage_case_insensitive(self) -> None:
        assert M._fact_coverage("UE INCLUDES PDU SESSION.", ["pdu session"]) == 1.0

    def test_fact_coverage_int_values(self) -> None:
        # YAML 数字 fact 自动 stringify
        assert M._fact_coverage("answer 240 kHz", [240, "kHz"]) == 1.0

    def test_forbidden_hits(self) -> None:
        assert M._forbidden_hits("contains 4G LTE.", ["4G", "5G"]) == ["4G"]

    def test_spec_match_hit(self) -> None:
        es = [{"spec_id": "38.211"}, {"spec_id": "23.501"}]
        assert M._spec_match(es, ["38.211", "23.502"]) == 1.0

    def test_spec_match_miss(self) -> None:
        es = [{"spec_id": "38.211"}]
        assert M._spec_match(es, ["23.502"]) == 0.0

    def test_spec_match_none_when_no_expected(self) -> None:
        assert M._spec_match([], ["38.211"]) is None


class TestChunkHydration:
    def test_hydrate_injects_content(self) -> None:
        cits = [
            {"chunk_id": "abc", "spec_id": "38.211"},
            {"chunk_id": "def", "spec_id": "23.501"},
            {"chunk_id": "missing", "spec_id": "29.500"},
        ]
        idx = {"abc": "this is chunk abc content", "def": "chunk def content"}
        out = M._hydrate_citations(cits, idx)
        assert out[0]["content"] == "this is chunk abc content"
        assert out[1]["content"] == "chunk def content"
        # missing chunk_id: no content key added
        assert "content" not in out[2]


class TestAgentResponseBuild:
    def test_build_basic(self) -> None:
        row = {"answer": "the answer", "terminal_event": "final", "duration_ms": 100}
        cits = [{"chunk_id": "x", "content": "ctx"}]
        resp = M._build_agent_response(row, cits)
        assert resp.answer == "the answer"
        assert resp.citations[0]["content"] == "ctx"
        assert resp.terminal_event == "final"
        assert resp.duration_ms == 100


class TestAggregate:
    def test_negative_pass_rate(self) -> None:
        rows = [
            {"category": "negative", "negative_judge_verdict": "VALID_REFUSAL"},
            {"category": "negative", "negative_judge_verdict": "VALID_REFUSAL"},
            {"category": "negative", "negative_judge_verdict": "PARTIAL_REFUSAL"},
            {"category": "negative", "negative_judge_verdict": "INVALID"},
            {"category": "definition", "negative_judge_verdict": None},
        ]
        out = M._negative_pass_rate(rows)
        assert out["n"] == 4
        assert out["valid"] == 2
        assert out["partial"] == 1
        assert out["invalid"] == 1
        # 2 + 0.5*1 = 2.5 / 4 = 0.625
        assert abs(out["weighted_pass_rate"] - 0.625) < 1e-6

    def test_aggregate_safe_mean_with_none(self) -> None:
        rows = [
            {"context_recall_spec": 1.0, "ragas_faithfulness": 0.8},
            {"context_recall_spec": 0.0, "ragas_faithfulness": None},
            {"context_recall_spec": None, "ragas_faithfulness": 0.6},
        ]
        agg = M._aggregate(rows)
        # spec_recall mean over [1.0, 0.0] = 0.5
        assert agg["context_recall_spec"] == 0.5
        # faith mean over [0.8, 0.6] = 0.7
        assert abs(agg["ragas_faithfulness"] - 0.7) < 1e-6


class TestBM25Index:
    def test_build_chunk_content_index(self, tmp_path) -> None:
        by_spec = tmp_path / "by_spec"
        by_spec.mkdir()
        (by_spec / "38.211.jsonl").write_text(
            json.dumps({"chunk_id": "c1", "content": "alpha"})
            + "\n"
            + json.dumps({"chunk_id": "c2", "content": "beta"})
            + "\n",
            encoding="utf-8",
        )
        idx = M.build_chunk_content_index(tmp_path, needed_specs={"38.211"})
        assert idx == {"c1": "alpha", "c2": "beta"}

    def test_missing_spec_logs_warn(self, tmp_path, caplog) -> None:
        by_spec = tmp_path / "by_spec"
        by_spec.mkdir()
        idx = M.build_chunk_content_index(tmp_path, needed_specs={"99.999"})
        assert idx == {}
