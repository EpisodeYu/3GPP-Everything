"""单测 `eval.runner`：SSE 消费 / 指标计算 / 聚合 / 报告 / mock HTTP run_eval。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml

from eval.retrieval.metrics import ExpectedSpec
from eval.runner import (
    AgentResponse,
    EvalResult,
    _fact_coverage,
    _forbidden_violations,
    _hits_for_metrics,
    _retrieved_sections,
    _retrieved_specs,
    aggregate,
    call_agent,
    compute_eval_metrics,
    consume_sse_stream,
    run_eval,
    write_report,
)
from eval.runner_retrieval import GoldenItem

# === helpers ===============================================================


async def _alines(lines: list[str]) -> AsyncIterator[str]:
    for line in lines:
        yield line


def _build_sse_lines(*events: tuple[str, str | dict]) -> list[str]:
    """构造 SSE 行流（不含 trailing \\n）。每帧 event:/data:/空行。"""
    out: list[str] = []
    for ev, data in events:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        out.append(f"event: {ev}")
        for line in payload.splitlines() or [""]:
            out.append(f"data: {line}")
        out.append("")
    return out


def _golden_item(
    *,
    item_id: str = "x-001",
    category: str = "definition",
    language: str = "en",
    expected_specs: list[tuple[str, list[str]]] | None = None,
    expected_facts: list[str] | None = None,
    forbidden: list[str] | None = None,
    must_say_not_found: bool = False,
) -> GoldenItem:
    specs = [
        ExpectedSpec(spec_id=sid, sections=tuple(secs)) for sid, secs in (expected_specs or [])
    ]
    return GoldenItem(
        id=item_id,
        category=category,
        language=language,
        question="q?",
        expected_specs=specs,
        expected_facts=expected_facts or [],
        forbidden=forbidden or [],
        must_say_not_found=must_say_not_found,
        source="hand_crafted",
    )


# === consume_sse_stream / _apply_event =====================================


class TestConsumeSseStream:
    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        resp = await consume_sse_stream(_alines([]))
        assert resp.answer == ""
        assert resp.terminal_event == "incomplete"

    @pytest.mark.asyncio
    async def test_full_happy_path(self) -> None:
        lines = _build_sse_lines(
            ("run_start", {"run_id": "r1"}),
            ("node_start", {"node": "retrieve"}),
            ("chunks_hit", {"chunks": [{"chunk_id": "c1", "spec_id": "38.331"}]}),
            ("node_end", {"node": "retrieve", "duration_ms": 42, "summary": ""}),
            (
                "chunks_rerank",
                {
                    "chunks": [
                        {"chunk_id": "c1", "spec_id": "38.331", "section_path": "5.3.5"},
                    ]
                },
            ),
            ("token", {"delta": "Hello "}),
            ("token", {"delta": "world."}),
            (
                "final",
                {
                    "answer": "Hello world.",
                    "citations": [{"spec_id": "38.331", "section_path": "5.3.5"}],
                    "confidence": 0.7,
                },
            ),
            ("end", {}),
        )
        resp = await consume_sse_stream(_alines(lines))
        assert resp.answer == "Hello world."
        assert resp.confidence == pytest.approx(0.7)
        assert resp.chunks_hit == [{"chunk_id": "c1", "spec_id": "38.331"}]
        assert len(resp.chunks_rerank) == 1
        assert resp.chunks_rerank[0]["section_path"] == "5.3.5"
        assert resp.node_durations_ms == {"retrieve": 42}
        assert resp.token_event_count == 2
        assert resp.terminal_event == "final"

    @pytest.mark.asyncio
    async def test_cancelled(self) -> None:
        lines = _build_sse_lines(
            ("run_start", {"run_id": "r1"}),
            ("cancelled", {"reason": "user_cancelled"}),
        )
        resp = await consume_sse_stream(_alines(lines))
        assert resp.terminal_event == "cancelled"

    @pytest.mark.asyncio
    async def test_error_event(self) -> None:
        lines = _build_sse_lines(
            ("run_start", {"run_id": "r1"}),
            ("error", {"code": "agent_failed", "message": "boom"}),
        )
        resp = await consume_sse_stream(_alines(lines))
        assert resp.terminal_event == "error"
        assert resp.error == {"code": "agent_failed", "message": "boom"}

    @pytest.mark.asyncio
    async def test_end_without_final_marks_end(self) -> None:
        lines = _build_sse_lines(("run_start", {"run_id": "r1"}), ("end", {}))
        resp = await consume_sse_stream(_alines(lines))
        assert resp.terminal_event == "end"

    @pytest.mark.asyncio
    async def test_malformed_json_data_ignored(self) -> None:
        # 不通过 _build_sse_lines；直接喂坏行
        lines = ["event: final", "data: this is not json", "", "event: end", "data: {}", ""]
        resp = await consume_sse_stream(_alines(lines))
        # final 没被 apply（data 解析失败）→ end 才设 terminal
        assert resp.answer == ""
        assert resp.terminal_event == "end"


# === metric helpers ========================================================


class TestFactCoverage:
    def test_empty_returns_none(self) -> None:
        assert _fact_coverage("anything", []) is None

    def test_full_hit(self) -> None:
        assert _fact_coverage("AMF and SMF and UPF", ["AMF", "SMF", "UPF"]) == 1.0

    def test_partial(self) -> None:
        assert _fact_coverage("only AMF here", ["AMF", "SMF"]) == 0.5

    def test_case_insensitive(self) -> None:
        assert _fact_coverage("contains amf", ["AMF"]) == 1.0


class TestForbiddenViolations:
    def test_empty_returns_empty(self) -> None:
        assert _forbidden_violations("anything", []) == []

    def test_no_hit(self) -> None:
        assert _forbidden_violations("AMF is correct", ["LTE", "GBA"]) == []

    def test_partial_hit(self) -> None:
        assert _forbidden_violations("uses LTE legacy", ["LTE", "GBA"]) == ["LTE"]

    def test_case_insensitive(self) -> None:
        assert _forbidden_violations("uses lte", ["LTE"]) == ["LTE"]


class TestRetrievedSpecs:
    def test_uses_rerank_first(self) -> None:
        resp = AgentResponse(
            chunks_hit=[{"spec_id": "23.501"}],
            chunks_rerank=[{"spec_id": "38.331"}, {"spec_id": "38.331"}, {"spec_id": "23.501"}],
        )
        # 去重保序
        assert _retrieved_specs(resp) == ["38.331", "23.501"]

    def test_fallback_to_hit(self) -> None:
        resp = AgentResponse(chunks_hit=[{"spec_id": "23.501"}])
        assert _retrieved_specs(resp) == ["23.501"]

    def test_fallback_to_citations(self) -> None:
        resp = AgentResponse(citations=[{"spec_id": "23.501"}])
        assert _retrieved_specs(resp) == ["23.501"]

    def test_empty(self) -> None:
        assert _retrieved_specs(AgentResponse()) == []


class TestRetrievedSections:
    def test_basic(self) -> None:
        resp = AgentResponse(
            chunks_rerank=[
                {"spec_id": "38.331", "section_path": "5.3.5"},
                {"spec_id": "38.331", "section_path": "5.3.5"},  # 去重
                {"spec_id": "23.501", "section_path": "4.3.2"},
            ]
        )
        assert _retrieved_sections(resp) == ["38.331 §5.3.5", "23.501 §4.3.2"]


class TestHitsForMetrics:
    def test_string_section_path_split(self) -> None:
        resp = AgentResponse(chunks_rerank=[{"spec_id": "38.331", "section_path": "5.3.5"}])
        hits = _hits_for_metrics(resp)
        assert len(hits) == 1
        assert hits[0].section_path == ("5", "3", "5")

    def test_list_section_path(self) -> None:
        resp = AgentResponse(chunks_rerank=[{"spec_id": "23.501", "section_path": ["4", "3", "2"]}])
        hits = _hits_for_metrics(resp)
        assert hits[0].section_path == ("4", "3", "2")

    def test_missing_spec_skipped(self) -> None:
        resp = AgentResponse(chunks_rerank=[{"chunk_id": "c1"}])
        assert _hits_for_metrics(resp) == []


# === compute_eval_metrics ==================================================


class TestComputeEvalMetrics:
    def test_definition_happy_path(self) -> None:
        item = _golden_item(
            expected_specs=[("38.331", ["5.3.5"])],
            expected_facts=["AMF", "SMF"],
            forbidden=["LTE"],
        )
        resp = AgentResponse(
            answer="AMF and SMF are NFs.",
            chunks_rerank=[{"spec_id": "38.331", "section_path": "5.3.5"}],
            citations=[{"spec_id": "38.331", "section_path": "5.3.5"}],
            terminal_event="final",
        )
        r = compute_eval_metrics(item, resp)
        assert r.context_recall_spec == 1.0
        assert r.context_recall_section == 1.0
        assert r.fact_coverage == 1.0
        assert r.forbidden_violations == []
        assert r.negative_judge_verdict is None
        assert r.negative_judge_reason is None
        assert r.terminal_event == "final"

    def test_section_miss_spec_hit(self) -> None:
        item = _golden_item(expected_specs=[("38.331", ["5.3.5"])])
        resp = AgentResponse(
            answer="x",
            chunks_rerank=[{"spec_id": "38.331", "section_path": "5.3.1"}],
            terminal_event="final",
        )
        r = compute_eval_metrics(item, resp)
        assert r.context_recall_spec == 1.0
        assert r.context_recall_section == 0.0

    def test_negative_compute_metrics_skips_recall_and_facts(self) -> None:
        """negative item 的 expected_specs=[] / expected_facts=[] → recall/facts None。

        2026-05-20 改口径后，compute_eval_metrics 不再写 verdict；负题的 judge
        verdict 由 run_eval 在外层调 NegativeJudge.score_item 填。
        """
        item = _golden_item(
            category="negative",
            language="en",
            forbidden=["LTE", "Turbo"],
            must_say_not_found=True,
        )
        resp = AgentResponse(answer="The spec does not define X here.", terminal_event="final")
        r = compute_eval_metrics(item, resp)
        assert r.context_recall_spec is None
        assert r.context_recall_section is None
        assert r.fact_coverage is None
        assert r.negative_judge_verdict is None  # 未注入 judge → None

    def test_negative_forbidden_still_reported(self) -> None:
        """forbidden_violations 仍是独立 metric，不再耦合 must_nf。"""
        item = _golden_item(
            category="negative",
            language="en",
            forbidden=["LTE"],
            must_say_not_found=True,
        )
        resp = AgentResponse(answer="not found — LTE is not used here", terminal_event="final")
        r = compute_eval_metrics(item, resp)
        assert r.forbidden_violations == ["LTE"]
        assert r.negative_judge_verdict is None

    def test_empty_answer(self) -> None:
        item = _golden_item(expected_facts=["AMF"])
        resp = AgentResponse(answer="", terminal_event="error")
        r = compute_eval_metrics(item, resp)
        assert r.fact_coverage == 0.0
        assert r.forbidden_violations == []
        assert r.terminal_event == "error"


# === aggregate + write_report ==============================================


def _eval_row(**kw) -> EvalResult:  # type: ignore[no-untyped-def]
    defaults = dict(
        item_id="x",
        category="definition",
        language="en",
        retrieved_specs=[],
        retrieved_sections=[],
        context_recall_spec=1.0,
        context_recall_section=1.0,
        answer="hi",
        citations=[],
        fact_coverage=1.0,
        forbidden_violations=[],
        duration_ms=100,
        terminal_event="final",
    )
    defaults.update(kw)
    return EvalResult(**defaults)


class TestAggregate:
    def test_empty(self) -> None:
        agg = aggregate([])
        assert agg["total"] == 0
        assert agg["context_recall_section"] is None
        assert agg["forbidden_violation_rate"] == 0.0

    def test_mix(self) -> None:
        rows = [
            _eval_row(item_id="a", category="definition", context_recall_section=1.0),
            _eval_row(item_id="b", category="definition", context_recall_section=0.0),
            _eval_row(
                item_id="c",
                category="negative",
                context_recall_section=None,
                context_recall_spec=None,
                fact_coverage=None,
                negative_judge_verdict="VALID_REFUSAL",
                forbidden_violations=[],
            ),
            _eval_row(
                item_id="d",
                category="negative",
                context_recall_section=None,
                fact_coverage=None,
                negative_judge_verdict="INVALID",
                forbidden_violations=["LTE"],
            ),
        ]
        agg = aggregate(rows)
        assert agg["total"] == 4
        assert agg["by_category"] == {"definition": 2, "negative": 2}
        # 只算 non-None 的 → (1+0)/2 = 0.5
        assert agg["context_recall_section"] == 0.5
        assert agg["forbidden_violation_rate"] == 0.25
        # 1 VALID + 1 INVALID → valid_rate = 0.5；unjudged 0
        assert agg["negative_judge"]["total"] == 2
        assert agg["negative_judge"]["verdict_counts"] == {
            "VALID_REFUSAL": 1,
            "PARTIAL_REFUSAL": 0,
            "INVALID": 1,
            "unjudged": 0,
        }
        assert agg["negative_judge"]["valid_rate"] == 0.5
        # weighted = (1 VALID + 0.5 × 0 PARTIAL) / 2 judged = 0.5
        assert agg["negative_judge"]["weighted_pass_rate"] == 0.5

    def test_negative_partial_weighted(self) -> None:
        """混合 VALID+PARTIAL+INVALID → weighted = (VALID + 0.5·PARTIAL) / judged。"""
        rows = [
            _eval_row(
                item_id=str(i),
                category="negative",
                context_recall_section=None,
                context_recall_spec=None,
                fact_coverage=None,
                negative_judge_verdict=v,
            )
            for i, v in enumerate(
                ["VALID_REFUSAL"] * 14 + ["PARTIAL_REFUSAL"] + ["INVALID"]
            )
        ]
        agg = aggregate(rows)
        # (14 + 0.5) / 16 = 0.90625
        assert agg["negative_judge"]["weighted_pass_rate"] == pytest.approx(14.5 / 16)
        assert agg["negative_judge"]["valid_rate"] == pytest.approx(14 / 16)

    def test_unjudged_negative(self) -> None:
        """negative item 但未注入 judge → unjudged 计数 +1，valid_rate=None。"""
        rows = [
            _eval_row(
                item_id="n1",
                category="negative",
                context_recall_section=None,
                context_recall_spec=None,
                fact_coverage=None,
                negative_judge_verdict=None,
            ),
        ]
        agg = aggregate(rows)
        assert agg["negative_judge"]["verdict_counts"]["unjudged"] == 1
        assert agg["negative_judge"]["valid_rate"] is None
        assert agg["negative_judge"]["weighted_pass_rate"] is None


class TestWriteReport:
    def test_writes_both_files(self, tmp_path: Path) -> None:
        rows = [
            _eval_row(item_id="ok", terminal_event="final"),
            _eval_row(
                item_id="bad",
                terminal_event="error",
                context_recall_section=0.0,
                forbidden_violations=["LTE"],
            ),
        ]
        write_report(rows, tmp_path)
        assert (tmp_path / "results.json").exists()
        assert (tmp_path / "report.md").exists()
        data = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
        assert data["aggregate"]["total"] == 2
        # report.md 应列出 bad 行
        md = (tmp_path / "report.md").read_text(encoding="utf-8")
        assert "bad" in md
        assert "ok" not in md.split("Failed / Notable items")[1]  # ok 不在异常段


# === run_eval (mock httpx) =================================================


def _write_minimal_golden(p: Path, items: list[dict]) -> None:
    doc = {
        "version": 1,
        "created_at": "2026-05-20",
        "total": len(items),
        "sources": ["hand_crafted"],
        "categories": ["definition", "negative"],
        "items": items,
    }
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _sse_body_text(*events: tuple[str, str | dict]) -> str:
    """生成完整 SSE 响应体（含 trailing 空行）。"""
    return "\n".join(_build_sse_lines(*events)) + "\n"


def _mock_transport(
    *,
    session_id: str = "s1",
    sse_body: str,
) -> httpx.MockTransport:
    """两次请求：POST /sessions → 201；POST /sessions/{sid}/messages → 200 SSE。"""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/sessions") and req.method == "POST":
            return httpx.Response(201, json={"id": session_id, "title": "t", "mode_default": "qa"})
        if req.method == "POST" and "/messages" in req.url.path:
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_call_agent_full_path() -> None:
    sse = _sse_body_text(
        ("run_start", {"run_id": "r1"}),
        ("chunks_rerank", {"chunks": [{"spec_id": "38.331", "section_path": "5.3"}]}),
        ("final", {"answer": "ok", "citations": [], "confidence": 0.5}),
        ("end", {}),
    )
    transport = _mock_transport(sse_body=sse)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await call_agent(client=client, auth_token="t", question="q?", api_prefix="/api/v1")
    assert resp.terminal_event == "final"
    assert resp.answer == "ok"
    assert len(resp.chunks_rerank) == 1


@pytest.mark.asyncio
async def test_run_eval_with_source_filter_and_subset(tmp_path: Path) -> None:
    items = [
        {
            "id": "def-1",
            "category": "definition",
            "language": "en",
            "source": "hand_crafted",
            "question": "what is AMF?",
            "expected_specs": [{"spec_id": "23.501", "sections": ["5.2.1"]}],
            "expected_facts": ["access and mobility"],
            "forbidden": ["LTE"],
            "must_say_not_found": False,
        },
        {
            "id": "def-2",
            "category": "definition",
            "language": "en",
            "source": "teleqna_transformed",
            "question": "what is SMF?",
            "expected_specs": [{"spec_id": "23.501", "sections": ["6.2.2"]}],
            "expected_facts": ["session management"],
            "forbidden": [],
            "must_say_not_found": False,
        },
        {
            "id": "def-3",
            "category": "definition",
            "language": "en",
            "source": "hand_crafted",
            "question": "what is UPF?",
            "expected_specs": [{"spec_id": "23.501", "sections": ["6.2.3"]}],
            "expected_facts": ["user plane"],
            "forbidden": [],
            "must_say_not_found": False,
        },
    ]
    golden = tmp_path / "v1.yaml"
    _write_minimal_golden(golden, items)

    sse = _sse_body_text(
        ("chunks_rerank", {"chunks": [{"spec_id": "23.501", "section_path": "5.2.1"}]}),
        ("final", {"answer": "access and mobility blah", "citations": [], "confidence": 0.6}),
        ("end", {}),
    )
    transport = _mock_transport(sse_body=sse)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = await run_eval(
            golden,
            client=client,
            auth_token="t",
            source_filter="hand_crafted",
            subset=1,
        )
    # source=hand_crafted 过滤 → 剩 def-1 / def-3；subset=1 → 取 def-1
    assert len(results) == 1
    assert results[0].item_id == "def-1"
    assert results[0].fact_coverage == 1.0


@pytest.mark.asyncio
async def test_run_eval_http_error_isolated(tmp_path: Path) -> None:
    items = [
        {
            "id": "def-1",
            "category": "definition",
            "language": "en",
            "source": "hand_crafted",
            "question": "?",
            "expected_specs": [],
            "expected_facts": [],
            "forbidden": [],
            "must_say_not_found": False,
        }
    ]
    golden = tmp_path / "v1.yaml"
    _write_minimal_golden(golden, items)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "internal"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = await run_eval(golden, client=client, auth_token="t")
    assert len(results) == 1
    assert results[0].terminal_event == "http_error"
    assert results[0].error is not None
