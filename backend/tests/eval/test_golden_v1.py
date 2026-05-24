"""M7.1 端到端 RAG runner 集成测 + D13 第一档（宽松）金标准阈值。

三个测试用途分工：

- `test_runner_smoke_against_canned_backend`：用 canned LangGraph 注入 + ASGITransport
  in-process backend，跑 eval.runner.run_eval(...)；**不调真实 LLM**。
  作用：catch backend SSE event 名/字段漂移与 runner expectation 的兼容性。
  覆盖：runner 能开 session、发 message、收完 10 类 event、产 EvalResult。

- `test_golden_v1_daily`：D13 第一档宽松（faithfulness/recall/answer_relevancy/correctness
  阈值见 06-...md §7）；需 `RUN_LIVE_EVAL=1` + 真 backend 端口或本地服务。否则 skip。

- `test_golden_v1_full`：每周一全集（M7 期间用宽松，M8 上线前 PR 收紧）。
  同样需 RUN_LIVE_EVAL=1 + 真 backend。

`make eval-daily` / `make eval-weekly` 即 `pytest -m eval -q`，命中本文件三个 case。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from eval.runner import EvalResult, run_eval, write_report
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_V1 = REPO_ROOT / "eval" / "golden" / "v1.yaml"


# === Canned LangGraph（复用 test_chat.py 同款结构，无 backend test 内部依赖） ===


class _CannedGraph:
    """喂出齐全的 10 类事件 + 完整 final_state 的 fake graph。"""

    def __init__(self, *, events: list[dict[str, Any]], final_state: dict[str, Any]) -> None:
        self._events = events
        self._final_state = final_state
        self.aupdate_state_calls: list[dict[str, Any]] = []

    async def astream_events(
        self, state: Any, *, config: Any, version: str
    ) -> AsyncIterator[dict[str, Any]]:
        for ev in self._events:
            yield ev
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "data": {"output": self._final_state},
        }

    async def aupdate_state(self, *, config: Any, values: dict[str, Any]) -> None:
        self.aupdate_state_calls.append({"config": config, "values": values})


def _full_canned_events() -> list[dict[str, Any]]:
    """retrieve→rerank→generate 完整 SSE 事件序列（覆盖 chunks_hit / chunks_rerank / token）。"""
    return [
        {"event": "on_chain_start", "name": "retrieve", "data": {}},
        {
            "event": "on_custom_event",
            "name": "chunks_hit",
            "data": {"chunks": [{"chunk_id": "c1", "spec_id": "23.501", "section_path": "5.2.1"}]},
        },
        {
            "event": "on_chain_end",
            "name": "retrieve",
            "data": {"output": {"candidates": [1]}},
        },
        {"event": "on_chain_start", "name": "rerank", "data": {}},
        {
            "event": "on_custom_event",
            "name": "chunks_rerank",
            "data": {
                "chunks": [
                    {
                        "chunk_id": "c1",
                        "spec_id": "23.501",
                        "section_path": "5.2.1",
                        "rerank_score": 0.92,
                    }
                ]
            },
        },
        {"event": "on_chain_end", "name": "rerank", "data": {"output": {"reranked": [1]}}},
        {"event": "on_chain_start", "name": "generate", "data": {}},
        {
            "event": "on_chat_model_stream",
            "name": "generate",
            "data": {"chunk": AIMessageChunk(content="AMF handles ")},
        },
        {
            "event": "on_chat_model_stream",
            "name": "generate",
            "data": {"chunk": AIMessageChunk(content="access and mobility.")},
        },
        {"event": "on_chain_end", "name": "generate", "data": {"output": {}}},
    ]


def _full_canned_final_state(answer: str) -> dict[str, Any]:
    return {
        "final_answer": answer,
        "citations": [
            {
                "chunk_id": "c1",
                "spec_id": "23.501",
                "section_path": "5.2.1",
                "rerank_score": 0.92,
            }
        ],
        "confidence": 0.8,
        "self_rag_verdict": "accept",
        "trace_id": "trace-eval-smoke",
        "cancelled": False,
    }


async def _bootstrap_admin_and_token(client: AsyncClient) -> str:
    """复制 test_auth 的最小流程，避免 cross-package import 风险。"""
    r = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "username": "admin1",
            "password": "passw0rd!",
            "invite_code": "invite-code-for-tests",
        },
    )
    assert r.status_code in (200, 201), r.text
    r = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin1", "password": "passw0rd!"},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["access_token"])


# === Smoke：runner ↔ in-process backend 集成 ===


@pytest.mark.eval
async def test_runner_smoke_against_canned_backend(app_and_state: Any, tmp_path: Path) -> None:
    """runner 能开 session + 发 message + 解析 SSE → EvalResult，事件 schema 对齐。

    用 canned graph 注入；不调真实 LLM；不依赖网络。本测试是 runner ↔ backend 契约的
    自动哨兵：backend 改 SSE event 名 / 字段，runner expectation 不匹配 → 本测试 fail。
    """
    app, _, _ = app_and_state
    app.state.agent_graph = _CannedGraph(
        events=_full_canned_events(),
        final_state=_full_canned_final_state("AMF handles access and mobility."),
    )

    # 写一份只含 1 道 hand_crafted golden 题（避免依赖 v1.yaml 的具体行）
    golden = tmp_path / "smoke.yaml"
    golden.write_text(
        """
version: 1
created_at: '2026-05-20'
total: 1
sources: ['hand_crafted']
categories: ['definition']
items:
  - id: smoke-001
    category: definition
    language: en
    source: hand_crafted
    question: What is AMF?
    expected_specs:
      - spec_id: "23.501"
        sections: ["5.2.1"]
    expected_facts:
      - access and mobility
    forbidden:
      - LTE
    must_say_not_found: false
""",
        encoding="utf-8",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _bootstrap_admin_and_token(client)
        results = await run_eval(
            golden,
            client=client,
            auth_token=token,
        )

    assert len(results) == 1
    r: EvalResult = results[0]
    assert r.item_id == "smoke-001"
    assert r.terminal_event == "final"
    assert r.answer == "AMF handles access and mobility."
    assert r.context_recall_spec == 1.0
    assert r.context_recall_section == 1.0
    assert r.fact_coverage == 1.0
    assert r.forbidden_violations == []
    assert r.negative_judge_verdict is None  # 非 negative item 且未注入 judge
    assert "23.501" in r.retrieved_specs
    assert any("5.2.1" in s for s in r.retrieved_sections)


# === D13 第一档：宽松 daily / 严格档 placeholder ===

_RUN_LIVE = os.getenv("RUN_LIVE_EVAL") == "1"


def _maybe_write_report(results: list[EvalResult], *, default: str) -> None:
    """落 results.json + report.md 到 EVAL_REPORT_DIR（M7.6 CI 上传 artifact 用）。

    本地不带 EVAL_REPORT_DIR 时落到 `default` 路径下，便于人翻历史 daily/weekly 报告。
    异常吞掉只 log（不能让磁盘问题把评测断言流程打断）。
    """
    outdir = Path(os.getenv("EVAL_REPORT_DIR") or default)
    try:
        write_report(results, outdir)
    except Exception as e:
        print(f"[eval] write_report failed (outdir={outdir}): {e}")


_LIVE_SKIP_REASON = (
    "需要 RUN_LIVE_EVAL=1 + 真 backend（带 LITELLM_API_KEY 等）；M7.6 CI 才正式触发。"
)


@pytest.mark.eval
@pytest.mark.skipif(not _RUN_LIVE, reason=_LIVE_SKIP_REASON)
async def test_golden_v1_daily() -> None:
    """每日 CI - daily 子集（source==hand_crafted，≥ 20 题，D13 宽松档）。"""
    from statistics import mean

    import httpx

    assert GOLDEN_V1.exists(), f"金标准缺失: {GOLDEN_V1}"

    base_url = os.environ.get("EVAL_BACKEND_BASE_URL", "http://localhost:8000")
    token = os.environ["EVAL_BACKEND_TOKEN"]  # 必填，daily 路径不做 bootstrap

    # 2026-05-20：negative 改用 LLM judge（VALID_REFUSAL / PARTIAL_REFUSAL / INVALID）
    # 详见 docs/04-handoff/2026-05-20-daily-eval-findings.md
    from eval.negative_judge import build_default_negative_judge

    judge = build_default_negative_judge()

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        results = await run_eval(
            GOLDEN_V1,
            client=client,
            auth_token=token,
            source_filter="hand_crafted",
            negative_judge=judge,
        )

    # M7.6 CI: 写 results.json + report.md 供 GH Actions artifact 上传；
    # 放在 assert 之前，保证阈值未达时报告仍能落盘
    _maybe_write_report(results, default="eval-results/m7-daily-latest")

    assert len(results) >= 20, f"daily 子集 < 20: {len(results)}"

    # D13 宽松档（06-...md §7）
    recalls = [r.context_recall_section for r in results if r.context_recall_section is not None]
    assert recalls, "无有效 context_recall_section 样本（全是 negative？）"
    avg_recall = mean(recalls)
    assert avg_recall >= 0.65, f"context recall 太低: {avg_recall}"

    # 负样本：宽松档 VALID_REFUSAL 比例 ≥ 0.85（PARTIAL 算 0.5）；
    # M8 严格档收紧到 1.0。详见 06-md §7。
    neg = [r for r in results if r.category == "negative"]
    if neg:
        valid = sum(1 for r in neg if r.negative_judge_verdict == "VALID_REFUSAL")
        partial = sum(1 for r in neg if r.negative_judge_verdict == "PARTIAL_REFUSAL")
        unjudged = sum(1 for r in neg if r.negative_judge_verdict is None)
        weighted_pass_rate = (valid + 0.5 * partial) / len(neg)
        assert unjudged == 0, f"negative judge 缺判定: {unjudged} 条未判（LLM 异常？）"
        assert weighted_pass_rate >= 0.85, (
            f"negative weighted pass rate 太低: {weighted_pass_rate:.2f}；"
            f"verdict 分布 VALID={valid} PARTIAL={partial} "
            f"INVALID={len(neg) - valid - partial - unjudged}"
        )


@pytest.mark.eval
@pytest.mark.skipif(not _RUN_LIVE, reason=_LIVE_SKIP_REASON)
async def test_golden_v1_full() -> None:
    """每周一 CI - 全集（M7 用宽松，M8 上线前 PR 把断言收紧至严格档）。"""
    from statistics import mean

    import httpx

    assert GOLDEN_V1.exists(), f"金标准缺失: {GOLDEN_V1}"

    base_url = os.environ.get("EVAL_BACKEND_BASE_URL", "http://localhost:8000")
    token = os.environ["EVAL_BACKEND_TOKEN"]

    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        results = await run_eval(GOLDEN_V1, client=client, auth_token=token)

    _maybe_write_report(results, default="eval-results/m7-weekly-latest")

    assert len(results) >= 140, f"全集题数不足 140: {len(results)}"

    recalls = [r.context_recall_section for r in results if r.context_recall_section is not None]
    avg_recall = mean(recalls) if recalls else 0.0
    # M7 期间仍走宽松（M8 上线前 PR 把这条 ≥ 0.65 改成 ≥ 0.80）
    assert avg_recall >= 0.65, f"全集 context recall 太低: {avg_recall}"
