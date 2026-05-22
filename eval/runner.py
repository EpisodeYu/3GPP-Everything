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
from typing import TYPE_CHECKING, Any

import httpx

# 2026-05-20：must_say_not_found_passed 由 substring 切到 LLM judge（详见
# docs/04-handoff/2026-05-20-daily-eval-findings.md），is_not_found_answer 不再用作
# metric；保留模块便于未来 suggest_questions 节点复用或回退实验。
from eval.retrieval.metrics import (
    HitRef,
    is_section_hit,
    is_spec_hit,
)
from eval.runner_retrieval import GoldenItem, load_golden
from eval.sse_parser import SSEEvent, SSEStreamParser

if TYPE_CHECKING:
    from eval.ragas_eval import RagasScorer

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
    # negative-判定（2026-05-20 由 substring 切到 LLM judge；非 negative item 永远 None）
    # verdict ∈ {"VALID_REFUSAL", "PARTIAL_REFUSAL", "INVALID", None}
    negative_judge_verdict: str | None = None
    negative_judge_reason: str | None = None
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
    # M7.3：当 langfuse 启用且 client 可用时，每条 result 关联一个幂等 trace_id；
    # 落 results.json 方便后续追到 Langfuse UI 上的对应 trace + score
    langfuse_trace_id: str | None = None


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

    # negative_judge_verdict 由 run_eval 在外层调 NegativeJudge.score_item 填；
    # compute_eval_metrics 本身只做纯函数指标，不打 LLM。
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
        duration_ms=resp.duration_ms,
        terminal_event=resp.terminal_event,
        error=resp.error,
    )


# === Orchestration =========================================================


def _apply_ragas_scores(result: EvalResult, scores: dict[str, float | None]) -> None:
    """把 ragas 单题 metric dict 写回 EvalResult 字段（缺的 → 维持 None）。"""
    if "ragas_faithfulness" in scores:
        result.ragas_faithfulness = scores["ragas_faithfulness"]
    if "ragas_answer_relevance" in scores:
        result.ragas_answer_relevance = scores["ragas_answer_relevance"]
    if "ragas_context_recall" in scores:
        result.ragas_context_recall = scores["ragas_context_recall"]
    if "ragas_context_precision" in scores:
        result.ragas_context_precision = scores["ragas_context_precision"]


_VERDICT_TO_NUMERIC: dict[str, float] = {
    "VALID_REFUSAL": 1.0,
    "PARTIAL_REFUSAL": 0.5,
    "INVALID": 0.0,
}


def _result_to_langfuse_scores(result: EvalResult) -> dict[str, float | bool | None]:
    """EvalResult → Langfuse score dict（None / NaN 留给 push_run_score 内部 skip）。

    口径：所有 metric 都用 NUMERIC（bool 由 push_run_score 转 0/1），统一一种 data_type
    便于 Cloud UI 上配 evaluator 阈值。

    2026-05-20：`must_say_not_found_passed` 替换为 `negative_judge_score`
    （VALID=1.0 / PARTIAL=0.5 / INVALID=0.0；None → 不上传）。
    """
    return {
        "context_recall_section": result.context_recall_section,
        "context_recall_spec": result.context_recall_spec,
        "fact_coverage": result.fact_coverage,
        "negative_judge_score": _VERDICT_TO_NUMERIC.get(result.negative_judge_verdict or ""),
        "forbidden_violation": 1.0 if result.forbidden_violations else 0.0,
        "ragas_faithfulness": result.ragas_faithfulness,
        "ragas_answer_relevance": result.ragas_answer_relevance,
        "ragas_context_recall": result.ragas_context_recall,
        "ragas_context_precision": result.ragas_context_precision,
    }


def _join_contexts(resp: AgentResponse, *, max_chars: int = 16000) -> str:
    """把 chunks_rerank（fallback chunks_hit）的完整 chunk 文本拼成单段 string。

    Langfuse Cloud 内置 faithfulness evaluator 模板要求 `{{context}}` 是
    "RAG 检索到的原文"。backend SSE `chunks_rerank` / `chunks_hit` 事件每条 chunk
    现在带两个字段：`content`（完整文本，2026-05-22 fixup-3 加）+ `preview`
    （前 240 字，留给前端流式展示）。runner 优先取 `content`，
    fallback `preview` / `text` 字段防御老 backend 部署或未来 schema 变更。

    每个 chunk 拼成：

        [<spec_id> §<section_path>]
        <full content>

    多 chunk 用 `\\n\\n---\\n\\n` 分隔；累计长度超过 `max_chars` 截断（防 token 爆掉
    evaluator judge 模型上下文）。`max_chars=16000` 默认 ≈ 16K 字符 ≈ 4-5K token，
    占 deepseek/gpt-4o-mini class judge 64K 上下文 < 10%，留足 prompt + response 余量。
    空字符串安全返回 ""。
    """
    chunks: list[dict] = list(resp.chunks_rerank or resp.chunks_hit or [])
    parts: list[str] = []
    total = 0
    for c in chunks:
        text = str(c.get("content") or c.get("preview") or c.get("text") or "").strip()
        if not text:
            continue
        sid = str(c.get("spec_id") or "").strip()
        sec_raw = c.get("section_path") or ""
        sec = ".".join(sec_raw) if isinstance(sec_raw, list) else str(sec_raw).strip()
        head = f"[{sid} §{sec}]" if sid or sec else "[chunk]"
        seg = f"{head}\n{text}"
        if total + len(seg) > max_chars:
            break
        parts.append(seg)
        total += len(seg)
    return "\n\n---\n\n".join(parts)


# Langfuse OTel span attribute keys（v4 SDK `langfuse._client.attributes`）。
# 按字面值常量化，避免依赖 SDK 私有模块导致升版本时炸。
# Cloud-side LLM-as-a-Judge evaluator 的 filter `experimentDatasetId any of ...`
# 直接读这些 OTel attribute 而不是数据库 `dataset_run_items` join；不写就永远
# 匹配不到。常量与 SDK 同步检查点：升 langfuse 主版本时 grep 校对一次。
_LF_OTEL_ENVIRONMENT = "langfuse.environment"
_LF_OTEL_EXPERIMENT_ID = "langfuse.experiment.id"
_LF_OTEL_EXPERIMENT_NAME = "langfuse.experiment.name"
_LF_OTEL_EXPERIMENT_DATASET_ID = "langfuse.experiment.dataset.id"
_LF_OTEL_EXPERIMENT_ITEM_ID = "langfuse.experiment.item.id"
_LF_OTEL_EXPERIMENT_ITEM_ROOT_OBSERVATION_ID = "langfuse.experiment.item.root_observation_id"
# Special environment value SDK uses for experiment runs（`_client/constants.py`）。
_LF_EXPERIMENT_ENVIRONMENT_VALUE = "sdk-experiment"


def _emit_langfuse_experiment_span(
    lf_client: Any,
    *,
    item: GoldenItem,
    resp: AgentResponse,
    dataset_id: str | None,
    dataset_name: str | None,
    run_label: str,
) -> tuple[str, str] | None:
    """创建一个 root SPAN observation 作为 experiment trace，写 5 个 experiment OTel
    attributes + environment=`sdk-experiment`，让 Cloud LLM-as-a-Judge evaluator
    的 filter `experimentDatasetId any of <id>` 能匹配。

    为什么不能继续用 `create_event`：
    - v3.175 Cloud evaluator 的 `experimentDatasetId` filter 是基于 trace 上的
      OTel attribute（`langfuse.experiment.dataset.id`），不是数据库 dataset_run_items 表
      的反向 join。`create_event` 不写这些 attribute，所以 evaluator 永远不触发。
    - SDK `dataset.run_experiment(task=...)` 内部就是这么干的（`langfuse/_client/client.py`
      第 2780-2810 行）：先 `start_as_current_observation` 创建 root span，再
      `set_attributes(EXPERIMENT_*)`。

    `output.contexts` 提供给 evaluator 的 `{{context}}` 变量映射 `Output $.contexts`。

    返回 `(trace_id, observation_id)` 给 caller 上报 score；client/SDK 异常时返回 None
    （runner 仍生成 EvalResult，仅本题不上报）。
    """
    contexts = _join_contexts(resp)
    span_input = {
        "question": item.question,
        "category": item.category,
        "language": item.language,
    }
    span_output = {
        "answer": resp.answer,
        "contexts": contexts,
        "terminal_event": resp.terminal_event,
        "citations": resp.citations,
    }
    span_metadata = {
        "item_id": item.id,
        "source": item.source,
        "dataset": dataset_name,
        "duration_ms": resp.duration_ms,
    }

    try:
        cm = lf_client.start_as_current_observation(
            name=f"eval-item-{item.id}",
            as_type="span",
            input=span_input,
            output=span_output,
            metadata=span_metadata,
        )
    except Exception as exc:  # pragma: no cover - 仅 SDK 不兼容
        log.warning("langfuse start_as_current_observation failed for %s: %s", item.id, exc)
        return None

    try:
        with cm as span:
            # 关键：写 experiment OTel attributes 让 evaluator filter 命中。
            # `_otel_span` 是 SDK 暴露的内部句柄；`set_attributes` 是 OTel 标准 API，
            # SDK 自己的 `run_experiment` 也直接用（见 client.py:2781）。
            attrs: dict[str, str] = {
                _LF_OTEL_ENVIRONMENT: _LF_EXPERIMENT_ENVIRONMENT_VALUE,
                _LF_OTEL_EXPERIMENT_ID: run_label,
                _LF_OTEL_EXPERIMENT_NAME: run_label,
                _LF_OTEL_EXPERIMENT_ITEM_ID: item.id,
                _LF_OTEL_EXPERIMENT_ITEM_ROOT_OBSERVATION_ID: span.id,
            }
            if dataset_id:
                attrs[_LF_OTEL_EXPERIMENT_DATASET_ID] = dataset_id
            try:
                span._otel_span.set_attributes(attrs)
            except Exception as exc:  # pragma: no cover
                log.warning("langfuse otel set_attributes failed for %s: %s", item.id, exc)
            return span.trace_id, span.id
    except Exception as exc:  # pragma: no cover
        log.warning("langfuse experiment span block failed for %s: %s", item.id, exc)
        return None


def _resolve_dataset_id(lf_client: Any, dataset_name: str | None) -> str | None:
    """通过 SDK 查 dataset 的内部 ID（experiment OTel attribute 需要 ID 不是 name）。

    缺 client / dataset_name 不存在 → 返回 None；caller 不会 set EXPERIMENT_DATASET_ID
    OTel attribute（evaluator filter `dataset any_of <id>` 不匹配，但 trace 仍能写出
    + dataset_run_items 仍可挂；属于优雅降级）。

    每次 run_eval 启动时调一次（不在循环里），结果传给每条 item 复用。
    """
    if lf_client is None or not dataset_name:
        return None
    try:
        ds = lf_client.api.datasets.get(dataset_name=dataset_name)
        return getattr(ds, "id", None)
    except Exception as exc:  # pragma: no cover - 仅网络/版本
        log.warning("langfuse datasets.get(%s) failed: %s", dataset_name, exc)
        return None


def _link_trace_to_dataset_run(
    lf_client: Any,
    *,
    trace_id: str,
    dataset_name: str | None,
    dataset_item_id: str,
    run_label: str,
) -> bool:
    """把 trace 关联到 Langfuse dataset run（v4 低层 REST：`api.dataset_run_items.create`）。

    Langfuse Cloud built-in evaluator 的 target=Experiments / Dataset Runs 在 UI 上
    通过 `dataset_run_items` 表索引到 trace；不创建 run_item → evaluator 永远查不到
    我们 push 上去的 trace。本函数与 `make_eval_trace_id` + `_emit_langfuse_trace_event`
    搭配：seed 决定 trace_id 幂等，run_name 决定一次 dataset run 的逻辑分组。

    `dataset_name` 为 None（runner 没传）时跳过：runner 仍生成 trace + score，但
    Cloud UI 上不会出现 Run（这种"不挂 dataset 的孤儿 run"是允许的回退路径）。

    任何异常吞掉转 log + 返回 False；调用方不依赖返回值，但保留布尔便于单测断言。
    """
    if not dataset_name:
        return False
    try:
        lf_client.api.dataset_run_items.create(
            run_name=run_label,
            dataset_item_id=dataset_item_id,
            trace_id=trace_id,
            metadata={"runner": "eval.runner.run_eval"},
        )
        return True
    except Exception as exc:  # pragma: no cover - 仅网络/版本异常
        log.warning(
            "langfuse dataset_run_items.create failed for %s/%s: %s",
            run_label,
            dataset_item_id,
            exc,
        )
        return False


async def run_eval(
    golden_path: Path,
    *,
    client: httpx.AsyncClient,
    auth_token: str,
    source_filter: str | None = None,
    subset: int | None = None,
    mode: str = "qa",
    api_prefix: str = "/api/v1",
    ragas_scorer: RagasScorer | None = None,
    negative_judge: Any | None = None,
    langfuse_run_label: str | None = None,
    langfuse_dataset_name: str | None = None,
    langfuse_client: Any | None = None,
) -> list[EvalResult]:
    """对 golden 集合的每条 item 跑端到端评测。

    顺序执行（不并发）：retrieval / generate 都吃 LLM token，并发省时不显著且增加
    cache 互相干扰；M7.1 求稳。M7.6 上 CI 时若耗时不满意再加并发。

    HTTP 异常单题：log + 生成空 EvalResult（terminal_event="http_error"），不阻塞后续。

    M7.2：`ragas_scorer` 传入时对每条 final/answer 非空的 result 跑 4 metric；
    单题 ragas 失败 → log + 该 metric None，不挂 runner。

    2026-05-20 negative_judge：`negative_judge` 传入时对每条 `must_say_not_found`
    item 跑一次三档枚举 LLM judge（VALID_REFUSAL / PARTIAL_REFUSAL / INVALID）。
    单题异常隔离同 ragas；不传则 verdict 字段保持 None。

    M7.3：`langfuse_run_label` 传入时启用 Langfuse 上报；缺 key / SDK / 网络都会让
    `eval.langfuse_dataset.get_client()` 返回 None → runner 自动 disable，原路径不受影响。
    `langfuse_client` 仅供测试注入用；生产路径调 `get_client()`。
    """
    items = load_golden(golden_path)
    if source_filter:
        items = [it for it in items if it.source == source_filter]
    if subset:
        items = items[:subset]

    lf_client: Any | None = None
    lf_dataset_id: str | None = None
    if langfuse_run_label is not None:
        if langfuse_client is not None:
            lf_client = langfuse_client
        else:
            from eval.langfuse_dataset import get_client as _get_lf_client

            lf_client = _get_lf_client()
        if lf_client is None:
            log.info(
                "langfuse_run_label=%s requested but client unavailable; disabled",
                langfuse_run_label,
            )
        else:
            # 一次性 lookup dataset.id，循环里复用（写 experiment OTel attribute 用）
            lf_dataset_id = _resolve_dataset_id(lf_client, langfuse_dataset_name)

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

        result = compute_eval_metrics(it, resp)
        if ragas_scorer is not None and resp.answer:
            try:
                scores = ragas_scorer.score_item(it, resp)
            except Exception as exc:
                # RagasScorer.score_item 内部已 try/except，此处兜底极端情况
                log.warning("ragas scorer crashed on %s: %s", it.id, exc)
                scores = {}
            _apply_ragas_scores(result, scores)

        if negative_judge is not None and it.must_say_not_found:
            try:
                judgement = negative_judge.score_item(it, resp)
            except Exception as exc:
                # NegativeJudge.score_item 内部已 try/except，此处兜底极端情况
                log.warning("negative_judge crashed on %s: %s", it.id, exc)
                judgement = {"verdict": None, "reason": f"runner_caught: {exc}"[:300]}
            result.negative_judge_verdict = judgement.get("verdict")
            result.negative_judge_reason = judgement.get("reason")

        if lf_client is not None and langfuse_run_label is not None:
            from eval.langfuse_dataset import push_run_score

            emitted = _emit_langfuse_experiment_span(
                lf_client,
                item=it,
                resp=resp,
                dataset_id=lf_dataset_id,
                dataset_name=langfuse_dataset_name,
                run_label=langfuse_run_label,
            )
            if emitted is not None:
                trace_id, _observation_id = emitted
                result.langfuse_trace_id = trace_id
                _link_trace_to_dataset_run(
                    lf_client,
                    trace_id=trace_id,
                    dataset_name=langfuse_dataset_name,
                    dataset_item_id=it.id,
                    run_label=langfuse_run_label,
                )
                push_run_score(
                    trace_id,
                    _result_to_langfuse_scores(result),
                    comment=f"run={langfuse_run_label} item={it.id}",
                    metadata={
                        "run_label": langfuse_run_label,
                        "item_id": it.id,
                        "dataset": langfuse_dataset_name,
                    },
                    client=lf_client,
                )

        results.append(result)
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
    verdict_counts = {"VALID_REFUSAL": 0, "PARTIAL_REFUSAL": 0, "INVALID": 0, "unjudged": 0}
    for r in neg:
        v = r.negative_judge_verdict
        if v in verdict_counts:
            verdict_counts[v] += 1
        else:
            verdict_counts["unjudged"] += 1
    judged = (
        verdict_counts["VALID_REFUSAL"]
        + verdict_counts["PARTIAL_REFUSAL"]
        + verdict_counts["INVALID"]
    )

    return {
        "total": len(results),
        "by_category": by_cat,
        "context_recall_section": _safe_mean([r.context_recall_section for r in results]),
        "context_recall_spec": _safe_mean([r.context_recall_spec for r in results]),
        "fact_coverage": _safe_mean([r.fact_coverage for r in results]),
        "forbidden_violation_rate": (
            sum(1 for r in results if r.forbidden_violations) / len(results) if results else 0.0
        ),
        "negative_judge": {
            "total": len(neg),
            "verdict_counts": verdict_counts,
            # valid_rate 分母按"已判定数"算，避免 judge 未注入时分母全 None 拉低分子
            "valid_rate": (verdict_counts["VALID_REFUSAL"] / judged) if judged else None,
            # weighted_pass_rate = (VALID + 0.5 × PARTIAL) / judged；
            # 与 backend/tests/eval/test_golden_v1.py daily 断言阈值（≥ 0.85）对齐
            "weighted_pass_rate": (
                (verdict_counts["VALID_REFUSAL"] + 0.5 * verdict_counts["PARTIAL_REFUSAL"]) / judged
                if judged
                else None
            ),
        },
        "ragas": {
            "faithfulness": _safe_mean([r.ragas_faithfulness for r in results]),
            "answer_relevance": _safe_mean([r.ragas_answer_relevance for r in results]),
            "context_recall": _safe_mean([r.ragas_context_recall for r in results]),
            "context_precision": _safe_mean([r.ragas_context_precision for r in results]),
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
    lines.append(f"- negative_judge: {agg['negative_judge']}")
    lines.append(f"- ragas: {agg.get('ragas')}")
    lines.append(f"- duration_p50_ms: {agg['duration_p50_ms']}")
    lines.append(f"- terminal_events: {agg['terminal_events']}")
    lines.append("")
    lines.append("## Failed / Notable items")
    lines.append("")
    for r in results:
        flagged = (
            r.terminal_event not in ("final",)
            or bool(r.forbidden_violations)
            or (r.negative_judge_verdict in ("PARTIAL_REFUSAL", "INVALID"))
            or (r.context_recall_section == 0.0)
        )
        if not flagged:
            continue
        verdict = r.negative_judge_verdict or "—"
        reason = (r.negative_judge_reason or "")[:120]
        lines.append(
            f"- **{r.item_id}** ({r.category}/{r.language}) terminal={r.terminal_event} "
            f"recall_section={r.context_recall_section} fact={r.fact_coverage} "
            f"forbidden={r.forbidden_violations} judge={verdict}"
            + (f" reason={reason!r}" if reason else "")
        )
    (outdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "AgentResponse",
    "EvalResult",
    "_apply_ragas_scores",
    "_emit_langfuse_experiment_span",
    "_join_contexts",
    "_link_trace_to_dataset_run",
    "_resolve_dataset_id",
    "_result_to_langfuse_scores",
    "aggregate",
    "call_agent",
    "compute_eval_metrics",
    "consume_sse_stream",
    "run_eval",
    "write_report",
]
