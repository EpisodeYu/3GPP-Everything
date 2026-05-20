"""单测 `eval.ragas_eval`：

覆盖（M7.2 验收）：
- _extract_contexts / _ground_truth 各种 fallback
- _coerce_score：float / NaN / None / 非数 → 正确归类
- RagasScorer.score_item：
    * happy path → 4 metric 全填
    * evaluate() 抛异常 → 全 None（单题失败容忍）
    * empty answer / contexts → 直接 None，不调 evaluate
    * scores 含 NaN / 缺 metric → 对应字段 None
- runner.run_eval 接入 ragas_scorer mock → EvalResult.ragas_* 字段被填
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from eval.ragas_eval import (
    RAGAS_METRIC_FIELDS,
    RagasScorer,
    _coerce_score,
    _empty_metric_dict,
    _extract_contexts,
    _ground_truth,
)
from eval.retrieval.metrics import ExpectedSpec
from eval.runner import AgentResponse, run_eval
from eval.runner_retrieval import GoldenItem


def _golden(
    *,
    item_id: str = "x-001",
    expected_facts: list[str] | None = None,
    expected_specs: list[tuple[str, list[str]]] | None = None,
) -> GoldenItem:
    return GoldenItem(
        id=item_id,
        category="definition",
        language="en",
        question="What is AMF?",
        expected_specs=[
            ExpectedSpec(spec_id=sid, sections=tuple(secs)) for sid, secs in (expected_specs or [])
        ],
        expected_facts=expected_facts or [],
        forbidden=[],
        must_say_not_found=False,
        source="hand_crafted",
    )


# === _extract_contexts ====================================================


class TestExtractContexts:
    def test_uses_rerank_first_content(self) -> None:
        resp = AgentResponse(
            chunks_hit=[{"content": "should-not-use"}],
            chunks_rerank=[{"content": "rerank text"}],
        )
        assert _extract_contexts(resp) == ["rerank text"]

    def test_text_field_alt(self) -> None:
        resp = AgentResponse(chunks_rerank=[{"text": "alt text"}])
        assert _extract_contexts(resp) == ["alt text"]

    def test_snippet_field(self) -> None:
        resp = AgentResponse(chunks_rerank=[{"snippet": "snip"}])
        assert _extract_contexts(resp) == ["snip"]

    def test_placeholder_when_no_content(self) -> None:
        """没原文 → 用 spec+section 占位，避免 ragas 收到空 contexts。"""
        resp = AgentResponse(chunks_rerank=[{"spec_id": "23.501", "section_path": "5.2.1"}])
        ctx = _extract_contexts(resp)
        assert ctx == ["23.501 §5.2.1"]

    def test_placeholder_list_section_path(self) -> None:
        resp = AgentResponse(chunks_rerank=[{"spec_id": "23.501", "section_path": ["5", "2", "1"]}])
        assert _extract_contexts(resp) == ["23.501 §5.2.1"]

    def test_fallback_to_hit_then_citations(self) -> None:
        resp = AgentResponse(chunks_hit=[{"content": "hit"}])
        assert _extract_contexts(resp) == ["hit"]
        resp2 = AgentResponse(citations=[{"content": "cit"}])
        assert _extract_contexts(resp2) == ["cit"]

    def test_empty(self) -> None:
        assert _extract_contexts(AgentResponse()) == []


# === _ground_truth ========================================================


class TestGroundTruth:
    def test_prefers_facts(self) -> None:
        item = _golden(expected_facts=["fact a", "fact b"])
        assert _ground_truth(item) == "fact a fact b"

    def test_fallback_to_specs(self) -> None:
        item = _golden(expected_specs=[("23.501", []), ("23.502", [])])
        assert _ground_truth(item) == "23.501 23.502"

    def test_empty_returns_placeholder(self) -> None:
        item = _golden()
        # negative 题这种情况
        assert _ground_truth(item) == "(no ground truth)"


# === _coerce_score ========================================================


class TestCoerceScore:
    def test_float_passes(self) -> None:
        assert _coerce_score(0.75) == 0.75

    def test_int_passes(self) -> None:
        assert _coerce_score(1) == 1.0

    def test_str_number(self) -> None:
        assert _coerce_score("0.5") == 0.5

    def test_none(self) -> None:
        assert _coerce_score(None) is None

    def test_nan(self) -> None:
        assert _coerce_score(float("nan")) is None

    def test_non_numeric(self) -> None:
        assert _coerce_score("not-a-number") is None
        assert _coerce_score(object()) is None


# === RagasScorer.score_item ===============================================


@dataclass
class _StubMetric:
    name: str


def _scorer_with_fake_evaluate(monkeypatch: pytest.MonkeyPatch, *, ev_factory) -> RagasScorer:
    """构造一个 RagasScorer，把 `ragas.evaluate` patch 成给定 factory。

    `ev_factory(dataset)` 应返回模拟的 EvaluationResult；抛异常会被 score_item 捕获。
    """

    def fake_evaluate(dataset, *, metrics, llm, embeddings, raise_exceptions, show_progress):
        return ev_factory(dataset)

    # 替换 ragas.evaluate（score_item 内部 from ragas import evaluate）
    import ragas

    monkeypatch.setattr(ragas, "evaluate", fake_evaluate)
    return RagasScorer(
        llm=object(),
        embeddings=object(),
        metrics=[
            _StubMetric("faithfulness"),
            _StubMetric("answer_relevancy"),
            _StubMetric("context_recall"),
            _StubMetric("context_precision"),
        ],
    )


class _FakeEvalResult:
    """伪 ragas EvaluationResult：暴露 .scores=[dict] 即可。"""

    def __init__(self, row: dict[str, Any]) -> None:
        self.scores = [row]


class TestRagasScorer:
    def test_happy_path_fills_all_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scorer = _scorer_with_fake_evaluate(
            monkeypatch,
            ev_factory=lambda ds: _FakeEvalResult(
                {
                    "faithfulness": 0.9,
                    "answer_relevancy": 0.8,
                    "context_recall": 0.7,
                    "context_precision": 0.6,
                }
            ),
        )
        item = _golden(expected_facts=["AMF handles mobility"])
        resp = AgentResponse(
            answer="AMF handles mobility.",
            chunks_rerank=[{"content": "AMF: access and mobility management function"}],
        )
        scores = scorer.score_item(item, resp)
        assert scores == {
            "ragas_faithfulness": 0.9,
            "ragas_answer_relevance": 0.8,
            "ragas_context_recall": 0.7,
            "ragas_context_precision": 0.6,
        }

    def test_nan_score_becomes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scorer = _scorer_with_fake_evaluate(
            monkeypatch,
            ev_factory=lambda ds: _FakeEvalResult(
                {
                    "faithfulness": 0.5,
                    "answer_relevancy": float("nan"),
                    "context_recall": None,
                    "context_precision": "not-a-number",
                }
            ),
        )
        item = _golden(expected_facts=["x"])
        resp = AgentResponse(
            answer="a",
            chunks_rerank=[{"content": "ctx"}],
        )
        scores = scorer.score_item(item, resp)
        assert scores["ragas_faithfulness"] == 0.5
        assert scores["ragas_answer_relevance"] is None
        assert scores["ragas_context_recall"] is None
        assert scores["ragas_context_precision"] is None

    def test_missing_metric_keys_in_scores_become_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ragas 返回的 row 只含 faithfulness；其他 3 个应填 None
        scorer = _scorer_with_fake_evaluate(
            monkeypatch,
            ev_factory=lambda ds: _FakeEvalResult({"faithfulness": 0.42}),
        )
        item = _golden(expected_facts=["x"])
        resp = AgentResponse(answer="a", chunks_rerank=[{"content": "ctx"}])
        scores = scorer.score_item(item, resp)
        assert scores["ragas_faithfulness"] == 0.42
        for f in [
            "ragas_answer_relevance",
            "ragas_context_recall",
            "ragas_context_precision",
        ]:
            assert scores[f] is None

    def test_evaluate_raises_returns_all_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ragas.evaluate 抛异常 → 单题 4 个 metric 都 None；不应 raise。"""

        def boom(ds):
            raise RuntimeError("simulated ragas crash")

        scorer = _scorer_with_fake_evaluate(monkeypatch, ev_factory=boom)
        item = _golden(expected_facts=["x"])
        resp = AgentResponse(answer="a", chunks_rerank=[{"content": "ctx"}])
        scores = scorer.score_item(item, resp)
        assert scores == _empty_metric_dict()

    def test_empty_answer_skips_evaluate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """answer 为空 → 直接返回全 None，不调 evaluate。"""
        called: list[int] = []

        scorer = _scorer_with_fake_evaluate(
            monkeypatch,
            ev_factory=lambda ds: called.append(1) or _FakeEvalResult({}),  # type: ignore[func-returns-value]
        )
        item = _golden(expected_facts=["x"])
        resp = AgentResponse(answer="", chunks_rerank=[{"content": "ctx"}])
        scores = scorer.score_item(item, resp)
        assert scores == _empty_metric_dict()
        assert called == []

    def test_empty_contexts_skips_evaluate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """contexts 为空 → 直接返回全 None，不调 evaluate。"""
        called: list[int] = []
        scorer = _scorer_with_fake_evaluate(
            monkeypatch,
            ev_factory=lambda ds: called.append(1) or _FakeEvalResult({}),  # type: ignore[func-returns-value]
        )
        item = _golden(expected_facts=["x"])
        # 没有任何 chunks_rerank / chunks_hit / citations
        resp = AgentResponse(answer="a non-empty answer")
        scores = scorer.score_item(item, resp)
        assert scores == _empty_metric_dict()
        assert called == []

    def test_extract_scores_falls_back_to_pandas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EvaluationResult 没 .scores 但有 .to_pandas() → 也能取到。"""

        class _PandasOnly:
            def to_pandas(self):
                import pandas as pd

                return pd.DataFrame(
                    [
                        {
                            "faithfulness": 0.3,
                            "answer_relevancy": 0.4,
                            "context_recall": 0.5,
                            "context_precision": 0.6,
                        }
                    ]
                )

        scorer = _scorer_with_fake_evaluate(monkeypatch, ev_factory=lambda ds: _PandasOnly())
        item = _golden(expected_facts=["x"])
        resp = AgentResponse(answer="a", chunks_rerank=[{"content": "ctx"}])
        scores = scorer.score_item(item, resp)
        assert scores["ragas_faithfulness"] == 0.3
        assert scores["ragas_answer_relevance"] == 0.4
        assert scores["ragas_context_recall"] == 0.5
        assert scores["ragas_context_precision"] == 0.6


# === RAGAS_METRIC_FIELDS shape check ======================================


def test_metric_fields_match_eval_result_attrs() -> None:
    """RagasScorer 输出的 4 个 key 必须能直接 setattr 到 EvalResult 上。"""
    from eval.runner import EvalResult

    er = EvalResult(
        item_id="x",
        category="definition",
        language="en",
        retrieved_specs=[],
        retrieved_sections=[],
        context_recall_spec=None,
        context_recall_section=None,
        answer="",
        citations=[],
        fact_coverage=None,
        forbidden_violations=[],
        must_say_not_found_passed=None,
    )
    # 确保 EvalResult 有这 4 个 attribute（slots=True 时缺一个就会 AttributeError）
    for f in RAGAS_METRIC_FIELDS:
        setattr(er, f, 0.5)
        assert getattr(er, f) == 0.5


# === runner.run_eval 接入 RagasScorer mock ===============================


async def _alines(lines: list[str]) -> AsyncIterator[str]:
    for line in lines:
        yield line


def _sse_body() -> str:
    chunk_payload = (
        '{"chunks": [{"spec_id": "23.501", "section_path": "5.2.1", "content": "AMF content"}]}'
    )
    lines = [
        "event: chunks_rerank",
        f"data: {chunk_payload}",
        "",
        "event: final",
        'data: {"answer": "AMF handles access and mobility.", "citations": [], "confidence": 0.8}',
        "",
        "event: end",
        "data: {}",
        "",
    ]
    return "\n".join(lines) + "\n"


def _write_minimal_golden(p: Path) -> None:
    doc = {
        "version": 1,
        "created_at": "2026-05-20",
        "total": 1,
        "sources": ["hand_crafted"],
        "categories": ["definition"],
        "items": [
            {
                "id": "def-1",
                "category": "definition",
                "language": "en",
                "source": "hand_crafted",
                "question": "What is AMF?",
                "expected_specs": [{"spec_id": "23.501", "sections": ["5.2.1"]}],
                "expected_facts": ["access and mobility"],
                "forbidden": [],
                "must_say_not_found": False,
            }
        ],
    }
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")


class _FakeScorer:
    """实现 RagasScorer.score_item 接口的 mock，记录调用次数。"""

    def __init__(self, scores: dict[str, float | None]) -> None:
        self.scores = scores
        self.calls: list[str] = []

    def score_item(self, item: GoldenItem, resp: AgentResponse) -> dict[str, float | None]:
        self.calls.append(item.id)
        return dict(self.scores)


def _mock_transport(*, session_id: str = "s1", sse_body: str) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/sessions") and req.method == "POST":
            return httpx.Response(201, json={"id": session_id})
        if "/messages" in req.url.path and req.method == "POST":
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_run_eval_applies_ragas_scorer(tmp_path: Path) -> None:
    golden = tmp_path / "g.yaml"
    _write_minimal_golden(golden)
    scorer = _FakeScorer(
        {
            "ragas_faithfulness": 0.91,
            "ragas_answer_relevance": 0.82,
            "ragas_context_recall": 0.73,
            "ragas_context_precision": 0.64,
        }
    )
    transport = _mock_transport(sse_body=_sse_body())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = await run_eval(
            golden,
            client=client,
            auth_token="t",
            ragas_scorer=scorer,
        )
    assert len(results) == 1
    r = results[0]
    assert r.ragas_faithfulness == 0.91
    assert r.ragas_answer_relevance == 0.82
    assert r.ragas_context_recall == 0.73
    assert r.ragas_context_precision == 0.64
    assert scorer.calls == ["def-1"]


@pytest.mark.asyncio
async def test_run_eval_without_scorer_keeps_none(tmp_path: Path) -> None:
    golden = tmp_path / "g.yaml"
    _write_minimal_golden(golden)
    transport = _mock_transport(sse_body=_sse_body())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = await run_eval(golden, client=client, auth_token="t")
    r = results[0]
    assert r.ragas_faithfulness is None
    assert r.ragas_answer_relevance is None
    assert r.ragas_context_recall is None
    assert r.ragas_context_precision is None


@pytest.mark.asyncio
async def test_run_eval_scorer_crash_does_not_break_runner(tmp_path: Path) -> None:
    """scorer.score_item 抛异常 → log + 该 result.ragas_* 维持 None；runner 不挂。"""
    golden = tmp_path / "g.yaml"
    _write_minimal_golden(golden)

    class _BoomScorer:
        def score_item(self, item: GoldenItem, resp: AgentResponse) -> dict[str, float | None]:
            raise RuntimeError("simulated ragas crash inside scorer")

    transport = _mock_transport(sse_body=_sse_body())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = await run_eval(
            golden,
            client=client,
            auth_token="t",
            ragas_scorer=_BoomScorer(),
        )
    r = results[0]
    assert r.terminal_event == "final"
    # 全部 None（未填）
    assert r.ragas_faithfulness is None
    assert r.ragas_context_recall is None
