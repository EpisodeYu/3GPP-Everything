"""端到端 RAG evaluator runner（M7.1）。

流程（每题）：
    POST /api/v1/sessions          → 拿 session_id
    POST /api/v1/sessions/{sid}/messages → 流式消费 SSE
    解析事件 → AgentResponse
    compute_eval_metrics(item, AgentResponse) → EvalResult

设计 / 约定：
- 不耦合具体 backend 部署；调用方传 `httpx.AsyncClient`（可走真实 HTTP 或 ASGITransport
  in-process）+ 已登录的 bearer token，runner 不做鉴权 / 启动 backend
- 每题一个独立 session（避免历史污染 retrieval）
- 缺 final event（cancelled / error / 流断）→ 仍生成 EvalResult，标 terminal_event 给后续聚合分析
- 单元测试覆盖：consume_sse_stream / compute_eval_metrics（pure 函数）
  端到端 integration：`backend/tests/eval/test_golden_v1.py` 用 ASGITransport

引用：docs/03-development/06-evaluation-and-observability.md §4
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

import httpx

from eval.not_found_phrases import is_not_found_answer
from eval.retrieval.metrics import (
    HitRef,
    is_section_hit,
    is_spec_hit,
)
from eval.runner_retrieval import GoldenItem, load_golden
from eval.sse_parser import SSEEvent, SSEStreamParser

log = logging.getLogger(__name__)


# === Dataclasses ===========================================================


@dataclass(slots=True)
class AgentResponse:
    """从 SSE 流还原的 agent 终态。"""

    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    chunks_hit: list[dict] = field(default_factory=list)
    chunks_rerank: list[dict] = field(default_factory=list)
    node_durations_ms: dict[str, int] = field(default_factory=dict)
    terminal_event: str = "incomplete"
    error: dict | None = None
    duration_ms: int = 0
    token_event_count: int = 0


@dataclass(slots=True)
class EvalResult:
    """单题评测结果（落 results.json 一行）。详见 06-...md §4。"""

    item_id: str
    category: str
    language: str
    # retrieval
    retrieved_specs: list[str]
    retrieved_sections: list[str]
    context_recall_spec: float | None
    context_recall_section: float | None
    # answer
    answer: str
    citations: list[dict]
    fact_coverage: float | None
    forbidden_violations: list[str]
    must_say_not_found_passed: bool | None
    # Ragas（M7.2 填）
    ragas_faithfulness: float | None = None
    ragas_answer_relevance: float | None = None
    ragas_context_recall: float | None = None
    ragas_context_precision: float | None = None
    # 性能
    duration_ms: int = 0
    llm_calls: int = 0
    total_cost_usd: float = 0.0
    # bookkeeping
    terminal_event: str = ""
    error: dict | None = None


# === SSE 消费 ==============================================================


def _apply_event(resp: AgentResponse, ev: SSEEvent) -> None:
    """单条 SSE 事件 → 更新 AgentResponse。未识别事件忽略。"""
    try:
        data = ev.parse_json() if ev.data else {}
    except ValueError:
        log.warning("sse event %s data not json, skipped: %r", ev.event, ev.data[:80])
        return

    if ev.event == "chunks_hit":
        chunks = data.get("chunks") or []
        if isinstance(chunks, list):
            resp.chunks_hit = list(chunks)
    elif ev.event == "chunks_rerank":
        chunks = data.get("chunks") or []
        if isinstance(chunks, list):
            resp.chunks_rerank = list(chunks)
    elif ev.event == "node_end":
        node = data.get("node")
        dur = data.get("duration_ms")
        if isinstance(node, str) and isinstance(dur, int):
            resp.node_durations_ms[node] = dur
    elif ev.event == "token":
        resp.token_event_count += 1
    elif ev.event == "final":
        resp.answer = str(data.get("answer") or "")
        cits = data.get("citations") or []
        if isinstance(cits, list):
            resp.citations = list(cits)
        try:
            resp.confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            resp.confidence = 0.0
        resp.terminal_event = "final"
    elif ev.event == "cancelled":
        resp.terminal_event = "cancelled"
    elif ev.event == "error":
        resp.terminal_event = "error"
        resp.error = data if isinstance(data, dict) else {"raw": str(data)}
    elif ev.event == "end":
        # `end` 是真正最后一条；只在没收到 final/cancelled/error 时填占位
        if resp.terminal_event == "incomplete":
            resp.terminal_event = "end"
    # run_start / node_start 不影响 metrics，忽略


async def consume_sse_stream(
    line_iter: AsyncIterator[str],
    *,
    started_at: float | None = None,
) -> AgentResponse:
    """逐行喂入 SSEStreamParser，输出 AgentResponse。

    `started_at`：time.perf_counter() 的开始点；填了就计算 duration_ms。
    """
    resp = AgentResponse()
    parser = SSEStreamParser()
    async for line in line_iter:
        parser.feed(line)
        for ev in parser.drain():
            _apply_event(resp, ev)
    for ev in parser.close():
        _apply_event(resp, ev)
    if started_at is not None:
        resp.duration_ms = int((time.perf_counter() - started_at) * 1000)
    return resp


# === HTTP 调用 =============================================================


async def call_agent(
    *,
    client: httpx.AsyncClient,
    auth_token: str,
    question: str,
    mode: str = "qa",
    session_title: str = "eval-run",
    api_prefix: str = "/api/v1",
) -> AgentResponse:
    """开 session + 发 message + 消费 SSE → AgentResponse。

    缺 final / cancelled / error / end 都不抛；通过 terminal_event 让聚合层定夺。
    HTTP 异常（非 2xx）会抛 httpx.HTTPStatusError，由调用方决定 retry / skip。
    """
    headers = {"Authorization": f"Bearer {auth_token}"}

    sess_resp = await client.post(
        f"{api_prefix}/sessions",
        json={"title": session_title, "mode_default": mode},
        headers=headers,
    )
    sess_resp.raise_for_status()
    sid = sess_resp.json()["id"]

    started_at = time.perf_counter()
    url = f"{api_prefix}/sessions/{sid}/messages"
    async with client.stream(
        "POST",
        url,
        json={"content": question, "mode": mode},
        headers=headers,
    ) as resp:
        resp.raise_for_status()
        return await consume_sse_stream(resp.aiter_lines(), started_at=started_at)


# === Metrics ==============================================================


def _retrieved_specs(resp: AgentResponse) -> list[str]:
    """从 chunks_rerank（fallback chunks_hit / citations）取 spec_id 列表，去重保序。"""
    sources: list[Iterable[dict]] = [resp.chunks_rerank, resp.chunks_hit, resp.citations]
    out: list[str] = []
    seen: set[str] = set()
    for src in sources:
        for c in src:
            sid = str(c.get("spec_id") or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
        if out:
            break
    return out


def _retrieved_sections(resp: AgentResponse) -> list[str]:
    """从 chunks_rerank（fallback chunks_hit / citations）取 'spec_id §section' 字串列表。"""
    sources: list[Iterable[dict]] = [resp.chunks_rerank, resp.chunks_hit, resp.citations]
    out: list[str] = []
    seen: set[str] = set()
    for src in sources:
        for c in src:
            sid = str(c.get("spec_id") or "").strip()
            sec = str(c.get("section_path") or "").strip()
            if not sid:
                continue
            key = f"{sid} §{sec}" if sec else sid
            if key not in seen:
                seen.add(key)
                out.append(key)
        if out:
            break
    return out


def _hits_for_metrics(resp: AgentResponse) -> list[HitRef]:
    """把 chunks_rerank / chunks_hit / citations 转 HitRef 给 is_spec_hit / is_section_hit。"""
    src: Iterable[dict] = resp.chunks_rerank or resp.chunks_hit or resp.citations
    out: list[HitRef] = []
    for c in src:
        sid = str(c.get("spec_id") or "").strip()
        if not sid:
            continue
        sec_raw = c.get("section_path") or ""
        if isinstance(sec_raw, list):
            sec_tuple = tuple(str(x) for x in sec_raw)
        else:
            # backend 用 '.' 拼接，eval ExpectedSpec.sections 用前缀字符串
            sec_tuple = tuple(s for s in str(sec_raw).split(".") if s)
        out.append(HitRef(spec_id=sid, section_path=sec_tuple))
    return out


def _fact_coverage(answer: str, expected_facts: list[str]) -> float | None:
    """expected_facts 中有多少子串命中 answer（case-insensitive）。

    空 list → None（不进聚合，避免拉低/拉高均值）。
    """
    if not expected_facts:
        return None
    hay = answer.lower()
    hits = sum(1 for f in expected_facts if f and f.lower() in hay)
    return hits / len(expected_facts)


def _forbidden_violations(answer: str, forbidden: list[str]) -> list[str]:
    """forbidden 中出现在 answer 里的子串（case-insensitive）。空 → []。"""
    hay = answer.lower()
    return [f for f in forbidden if f and f.lower() in hay]


def compute_eval_metrics(item: GoldenItem, resp: AgentResponse) -> EvalResult:
    """纯函数：(golden_item, agent_response) → EvalResult。"""
    answer = resp.answer
    expected_specs = item.expected_specs

    # retrieval recall（negative 题 expected_specs=[] → None，不进聚合）
    if expected_specs:
        hits = _hits_for_metrics(resp)
        spec_hit = any(is_spec_hit(expected_specs, h) for h in hits) if hits else False
        section_hit = (
            any(any(is_section_hit(e, h) for e in expected_specs) for h in hits) if hits else False
        )
        recall_spec: float | None = 1.0 if spec_hit else 0.0
        recall_section: float | None = 1.0 if section_hit else 0.0
    else:
        recall_spec = None
        recall_section = None

    fact_cov = _fact_coverage(answer, item.expected_facts)
    violations = _forbidden_violations(answer, item.forbidden)

    if item.must_say_not_found:
        must_passed: bool | None = is_not_found_answer(answer, item.language) and not violations
    else:
        must_passed = None

    return EvalResult(
        item_id=item.id,
        category=item.category,
        language=item.language,
        retrieved_specs=_retrieved_specs(resp),
        retrieved_sections=_retrieved_sections(resp),
        context_recall_spec=recall_spec,
        context_recall_section=recall_section,
        answer=answer,
        citations=resp.citations,
        fact_coverage=fact_cov,
        forbidden_violations=violations,
        must_say_not_found_passed=must_passed,
        duration_ms=resp.duration_ms,
        terminal_event=resp.terminal_event,
        error=resp.error,
    )


# === Orchestration =========================================================


async def run_eval(
    golden_path: Path,
    *,
    client: httpx.AsyncClient,
    auth_token: str,
    source_filter: str | None = None,
    subset: int | None = None,
    mode: str = "qa",
    api_prefix: str = "/api/v1",
) -> list[EvalResult]:
    """对 golden 集合的每条 item 跑端到端评测。

    顺序执行（不并发）：retrieval / generate 都吃 LLM token，并发省时不显著且增加
    cache 互相干扰；M7.1 求稳。M7.6 上 CI 时若耗时不满意再加并发。

    HTTP 异常单题：log + 生成空 EvalResult（terminal_event="http_error"），不阻塞后续。
    """
    items = load_golden(golden_path)
    if source_filter:
        items = [it for it in items if it.source == source_filter]
    if subset:
        items = items[:subset]

    results: list[EvalResult] = []
    for it in items:
        try:
            resp = await call_agent(
                client=client,
                auth_token=auth_token,
                question=it.question,
                mode=mode,
                api_prefix=api_prefix,
            )
        except httpx.HTTPStatusError as exc:
            log.exception("agent http error on %s", it.id)
            resp = AgentResponse(
                terminal_event="http_error",
                error={"status": exc.response.status_code, "text": exc.response.text[:500]},
            )
        except Exception as exc:
            log.exception("agent call failed on %s", it.id)
            resp = AgentResponse(terminal_event="error", error={"exc": str(exc)})
        results.append(compute_eval_metrics(it, resp))
    return results


# === Reports ==============================================================


def _safe_mean(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return mean(xs) if xs else None


def aggregate(results: list[EvalResult]) -> dict[str, Any]:
    """聚合 results → 报告用 dict。"""
    by_cat: dict[str, int] = {}
    for r in results:
        by_cat[r.category] = by_cat.get(r.category, 0) + 1

    neg = [r for r in results if r.category == "negative"]
    neg_passed = [r for r in neg if r.must_say_not_found_passed]

    return {
        "total": len(results),
        "by_category": by_cat,
        "context_recall_section": _safe_mean([r.context_recall_section for r in results]),
        "context_recall_spec": _safe_mean([r.context_recall_spec for r in results]),
        "fact_coverage": _safe_mean([r.fact_coverage for r in results]),
        "forbidden_violation_rate": (
            sum(1 for r in results if r.forbidden_violations) / len(results) if results else 0.0
        ),
        "must_say_not_found": {
            "total": len(neg),
            "passed": len(neg_passed),
            "pass_rate": (len(neg_passed) / len(neg)) if neg else None,
        },
        "duration_p50_ms": (
            sorted(r.duration_ms for r in results)[len(results) // 2] if results else 0
        ),
        "terminal_events": {
            ev: sum(1 for r in results if r.terminal_event == ev)
            for ev in {r.terminal_event for r in results}
        },
    }


def write_report(results: list[EvalResult], outdir: Path) -> None:
    """outdir/results.json + outdir/report.md。"""
    outdir.mkdir(parents=True, exist_ok=True)
    agg = aggregate(results)
    (outdir / "results.json").write_text(
        json.dumps(
            {"aggregate": agg, "results": [asdict(r) for r in results]},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append(f"# Eval run — {len(results)} items")
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- total: {agg['total']}")
    lines.append(f"- by_category: {agg['by_category']}")
    lines.append(f"- context_recall_section: {agg['context_recall_section']}")
    lines.append(f"- context_recall_spec: {agg['context_recall_spec']}")
    lines.append(f"- fact_coverage: {agg['fact_coverage']}")
    lines.append(f"- forbidden_violation_rate: {agg['forbidden_violation_rate']}")
    lines.append(f"- must_say_not_found: {agg['must_say_not_found']}")
    lines.append(f"- duration_p50_ms: {agg['duration_p50_ms']}")
    lines.append(f"- terminal_events: {agg['terminal_events']}")
    lines.append("")
    lines.append("## Failed / Notable items")
    lines.append("")
    for r in results:
        flagged = (
            r.terminal_event not in ("final",)
            or bool(r.forbidden_violations)
            or (r.must_say_not_found_passed is False)
            or (r.context_recall_section == 0.0)
        )
        if not flagged:
            continue
        lines.append(
            f"- **{r.item_id}** ({r.category}/{r.language}) terminal={r.terminal_event} "
            f"recall_section={r.context_recall_section} fact={r.fact_coverage} "
            f"forbidden={r.forbidden_violations} must_nf={r.must_say_not_found_passed}"
        )
    (outdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "AgentResponse",
    "EvalResult",
    "aggregate",
    "call_agent",
    "compute_eval_metrics",
    "consume_sse_stream",
    "run_eval",
    "write_report",
]
