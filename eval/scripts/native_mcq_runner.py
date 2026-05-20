"""TeleQnA 原生选择题对照评测（M7.2）。

目的：让 LLM **不带 RAG** 直接做选择题，得到模型"裸知识"对 telecom 题的准确率
基线。对照两个模型：mimo-v2.5 + glm-5.1（都在 LiteLLM 中），均 temperature=0。
报告 LLM 选对 %，作为 RAG 端到端指标的下限锚（"RAG 至少要打过裸 LLM"）。

输入：`eval/teleqna/data/filtered/filtered.jsonl`（whitelist 命中的 17 篇相关题）
输出：
    eval-results/m7-native-mcq/{ts}/results.json
    eval-results/m7-native-mcq/{ts}/report.md

设计：
- 复用 `eval.teleqna.infer._LiteLLMChatClient` + `_RpmLimiter`（已 battle-tested）
- 每题 → 同一 prompt 喂两个模型各跑 1 次；记录选择 + 是否正确
- 失败容忍：单题 LLM 异常 → 记 `error`，模型不计为该题"答对"也不挂整体
- 不并发跑两模型间（顺序，让 _RpmLimiter 简单）；并发跑同模型多题

模块拆点（便于单测 mock LLM）：
- `parse_mcq_answer(text)` → "option N" | None：从模型自由文本里抽 option
- `parse_correct_option(answer)` → "option N" | None：从 TeleQnA `answer` 字段抽
- `score_item(predicted, correct)` → bool
- `evaluate_model_async(items, client, ...)` → list[ModelItemResult]
- `aggregate_results(by_model)` → 报告 dict
- `write_report(out_dir, ...)` → 写 markdown / json
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from eval.settings import EvalSettings, get_settings
from eval.teleqna.infer import (
    DEFAULT_CONCURRENT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_RPM,
    DEFAULT_TIMEOUT_S,
    _LiteLLMChatClient,
    _RpmLimiter,
)
from eval.teleqna.prompts import options_from_item

log = logging.getLogger(__name__)

# === Constants ============================================================

MCQ_SYSTEM_PROMPT = """You are a 3GPP telecommunications expert. \
You will be given a multiple-choice question with N options.

Pick the single best answer. Respond with ONLY one line in this exact format:
ANSWER: option K

where K is the option number (1, 2, 3, ...). Do not output any explanation, \
no markdown fences, no preamble. If genuinely unsure, still pick the most likely option.
"""

MCQ_USER_TEMPLATE = """Question: {question}

Options:
{options_block}

Pick the best answer and respond with `ANSWER: option K`."""


# 抽取"option N"的两个常见格式：
#   "ANSWER: option 3"  /  "Option 3"  /  "option 3: TS xxx"
_OPTION_RE = re.compile(r"\boption\s*(\d+)\b", re.IGNORECASE)
# 兜底：纯数字 "3" / "3." / "(3)"
_BARE_DIGIT_RE = re.compile(r"^\s*[\[\(]?\s*(\d+)\s*[\.\)\]]?\s*$")


# === Public parsers (单测点) ==============================================


def parse_mcq_answer(text: str) -> str | None:
    """从 LLM 自由文本里抽 'option N' → 规范化为 'option N' 字符串。

    返回 None 表示无法解析。模型偶尔输出 markdown / 多 option 时，取 *第一个* 命中。
    """
    if not text:
        return None
    m = _OPTION_RE.search(text)
    if m:
        return f"option {int(m.group(1))}"
    # 单行裸数字（少见，但 reasoning 模型有时省 prefix）
    bare = _BARE_DIGIT_RE.match(text)
    if bare:
        return f"option {int(bare.group(1))}"
    return None


def parse_correct_option(answer: str) -> str | None:
    """从 TeleQnA `answer` 字段（如 'option 3: TS 23.303'）抽出 'option N'。"""
    return parse_mcq_answer(answer or "")


def score_item(predicted: str | None, correct: str | None) -> bool:
    """单题判定：预测与正确选项一致才算对；任一缺 → False。"""
    if not predicted or not correct:
        return False
    return predicted.lower() == correct.lower()


# === Data classes =========================================================


@dataclass(slots=True)
class ModelItemResult:
    """单题在单模型上的评测结果。"""

    item_id: str
    model: str
    predicted: str | None
    correct: str | None
    is_correct: bool
    raw_response: str | None = None
    error: str | None = None
    elapsed_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class ModelAggregate:
    """单模型在整批上的聚合。"""

    model: str
    total: int = 0
    parsed: int = 0  # LLM 给出可解析的 option
    correct: int = 0
    errors: int = 0
    prompt_tokens_total: int = 0
    completion_tokens_total: int = 0
    elapsed_s: float = 0.0

    @property
    def accuracy(self) -> float | None:
        """correct / total；total=0 → None。"""
        return (self.correct / self.total) if self.total else None

    @property
    def parse_rate(self) -> float | None:
        return (self.parsed / self.total) if self.total else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "total": self.total,
            "parsed": self.parsed,
            "correct": self.correct,
            "errors": self.errors,
            "accuracy": self.accuracy,
            "parse_rate": self.parse_rate,
            "prompt_tokens_total": self.prompt_tokens_total,
            "completion_tokens_total": self.completion_tokens_total,
            "elapsed_s": round(self.elapsed_s, 1),
        }


# === LLM 调用 =============================================================


def _build_messages(item: dict) -> list[dict[str, str]]:
    options = options_from_item(item)
    options_block = "\n".join(f"  {k}: {v}" for k, v in options.items()) if options else "  (none)"
    user = MCQ_USER_TEMPLATE.format(
        question=str(item.get("question", "")).strip(),
        options_block=options_block,
    )
    return [
        {"role": "system", "content": MCQ_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


async def _answer_one(
    item: dict,
    *,
    client: _LiteLLMChatClient,
    limiter: _RpmLimiter,
    semaphore: asyncio.Semaphore,
    max_retries: int = 2,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ModelItemResult:
    """单题 → 调 LLM → 解析 → ModelItemResult。失败 → error 字段。"""
    item_id = str(item.get("id") or "")
    correct = parse_correct_option(str(item.get("answer", "")))
    messages = _build_messages(item)

    async with semaphore:
        await limiter.acquire()
        t0 = time.perf_counter()
        try:
            async for attempt in AsyncRetrying(
                reraise=True,
                stop=stop_after_attempt(max_retries + 1),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                retry=retry_if_exception_type(
                    (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException)
                ),
            ):
                with attempt:
                    payload = await client.chat(messages=messages, max_tokens=max_tokens)
        except RetryError as exc:
            last = exc.last_attempt.exception()
            return ModelItemResult(
                item_id=item_id,
                model=client.model,
                predicted=None,
                correct=correct,
                is_correct=False,
                error=f"http retries exhausted: {type(last).__name__}: {last}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
            return ModelItemResult(
                item_id=item_id,
                model=client.model,
                predicted=None,
                correct=correct,
                is_correct=False,
                error=f"http error: {type(exc).__name__}: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        choice = (payload.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        elapsed_ms = (time.perf_counter() - t0) * 1000

        predicted = parse_mcq_answer(content)
        return ModelItemResult(
            item_id=item_id,
            model=client.model,
            predicted=predicted,
            correct=correct,
            is_correct=score_item(predicted, correct),
            raw_response=content,
            elapsed_ms=elapsed_ms,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


async def evaluate_model_async(
    items: Iterable[dict],
    *,
    client: _LiteLLMChatClient,
    rpm: int = DEFAULT_RPM,
    concurrent: int = DEFAULT_CONCURRENT,
    progress_every: int = 50,
) -> tuple[list[ModelItemResult], ModelAggregate]:
    """对一个模型跑整批 → (per_item, aggregate)。"""
    items_list = list(items)
    limiter = _RpmLimiter(rpm)
    sem = asyncio.Semaphore(int(concurrent))
    log.info(
        "native_mcq start: model=%s total=%d rpm=%d concurrent=%d",
        client.model,
        len(items_list),
        rpm,
        concurrent,
    )

    t_all = time.perf_counter()
    tasks = [
        asyncio.create_task(_answer_one(it, client=client, limiter=limiter, semaphore=sem))
        for it in items_list
    ]
    results: list[ModelItemResult] = []
    agg = ModelAggregate(model=client.model)
    for done_idx, fut in enumerate(asyncio.as_completed(tasks), start=1):
        r = await fut
        results.append(r)
        agg.total += 1
        agg.prompt_tokens_total += r.prompt_tokens
        agg.completion_tokens_total += r.completion_tokens
        if r.error:
            agg.errors += 1
        if r.predicted is not None:
            agg.parsed += 1
        if r.is_correct:
            agg.correct += 1
        if done_idx % progress_every == 0:
            log.info(
                "native_mcq[%s] %d / %d | acc=%.3f parsed=%.3f errors=%d",
                client.model,
                done_idx,
                len(items_list),
                (agg.correct / max(agg.total, 1)),
                (agg.parsed / max(agg.total, 1)),
                agg.errors,
            )
    agg.elapsed_s = time.perf_counter() - t_all
    log.info("native_mcq done[%s]: %s", client.model, agg.to_dict())
    return results, agg


# === Report ===============================================================


def write_report(
    out_dir: Path,
    *,
    per_model_results: dict[str, list[ModelItemResult]],
    aggregates: list[ModelAggregate],
    input_path: Path,
    n_items: int,
) -> Path:
    """写 report.md + results.json；返回 report.md 路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    md_path = out_dir / "report.md"
    json_path = out_dir / "results.json"

    json_path.write_text(
        json.dumps(
            {
                "ts": ts,
                "input_path": str(input_path),
                "n_items": n_items,
                "aggregates": [a.to_dict() for a in aggregates],
                "per_model": {
                    model: [
                        {
                            "item_id": r.item_id,
                            "predicted": r.predicted,
                            "correct": r.correct,
                            "is_correct": r.is_correct,
                            "error": r.error,
                            "elapsed_ms": round(r.elapsed_ms, 1),
                        }
                        for r in results
                    ]
                    for model, results in per_model_results.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append(f"# TeleQnA 原生 MCQ 对照评测（{ts[:19]}）")
    lines.append("")
    lines.append(f"- input: `{input_path}`")
    lines.append(f"- n_items: {n_items}")
    lines.append("- 维度：LLM **裸知识** 选择题准确率（不接 RAG，作为 RAG 下限锚）")
    lines.append("")
    lines.append("## 模型对照")
    lines.append("")
    lines.append(
        "| model | total | correct | accuracy | parsed | parse_rate | errors | prompt_tok | completion_tok | elapsed_s |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for a in aggregates:
        acc = f"{a.accuracy:.3f}" if a.accuracy is not None else "—"
        pr = f"{a.parse_rate:.3f}" if a.parse_rate is not None else "—"
        lines.append(
            f"| {a.model} | {a.total} | {a.correct} | {acc} | "
            f"{a.parsed} | {pr} | {a.errors} | "
            f"{a.prompt_tokens_total} | {a.completion_tokens_total} | {a.elapsed_s:.1f} |"
        )
    lines.append("")
    lines.append("## 解读")
    lines.append("")
    lines.append("- accuracy = correct / total（含 parse 失败题，按答错算）")
    lines.append(
        "- parse_rate = 模型给出可解析 `option N` 的比例；偏低说明 prompt / 模型偏 reasoning 输出"
    )
    lines.append(
        "- RAG 端到端 fact_coverage / 答案正确率应**显著高于**此基线，否则检索没有带来知识增量"
    )
    lines.append("")
    lines.append(
        f"_报告由 `eval/scripts/native_mcq_runner.py` 自动生成；JSON 详情见 `{json_path.name}`。_"
    )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("wrote report: %s + %s", md_path, json_path)
    return md_path


# === Orchestration =======================================================


def load_filtered_items(
    input_path: Path,
    *,
    limit: int = 0,
) -> list[dict]:
    """读 filtered.jsonl（每行一条 JSON）。`limit > 0` 截断。"""
    items: list[dict] = []
    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
            if limit and len(items) >= limit:
                break
    return items


def _build_client(model: str, settings: EvalSettings | None = None) -> _LiteLLMChatClient:
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise RuntimeError("LITELLM_API_KEY missing in env / .env")
    return _LiteLLMChatClient(
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        model=model,
        timeout_s=DEFAULT_TIMEOUT_S,
    )


async def run_native_mcq_async(
    *,
    input_path: Path,
    out_dir: Path,
    models: list[str],
    limit: int = 0,
    rpm: int = DEFAULT_RPM,
    concurrent: int = DEFAULT_CONCURRENT,
    settings: EvalSettings | None = None,
) -> Path:
    """跑多模型 → 写报告。返回 report.md 路径。"""
    items = load_filtered_items(input_path, limit=limit)
    log.info("loaded %d items from %s (limit=%d)", len(items), input_path, limit)
    per_model_results: dict[str, list[ModelItemResult]] = {}
    aggregates: list[ModelAggregate] = []
    for model in models:
        client = _build_client(model, settings)
        try:
            results, agg = await evaluate_model_async(
                items, client=client, rpm=rpm, concurrent=concurrent
            )
        finally:
            await client.aclose()
        per_model_results[model] = results
        aggregates.append(agg)
    return write_report(
        out_dir,
        per_model_results=per_model_results,
        aggregates=aggregates,
        input_path=input_path,
        n_items=len(items),
    )


# === Aggregation helpers (单测点) =========================================


def aggregate_results(per_model_results: dict[str, list[ModelItemResult]]) -> list[ModelAggregate]:
    """从 per_model_results dict 重建聚合（独立于 evaluate_model_async 的内部累加）。

    用于：单测构造一批 ModelItemResult → 验证 accuracy / parse_rate / errors 计算。
    """
    out: list[ModelAggregate] = []
    for model, results in per_model_results.items():
        agg = ModelAggregate(model=model)
        for r in results:
            agg.total += 1
            agg.prompt_tokens_total += r.prompt_tokens
            agg.completion_tokens_total += r.completion_tokens
            if r.error:
                agg.errors += 1
            if r.predicted is not None:
                agg.parsed += 1
            if r.is_correct:
                agg.correct += 1
        out.append(agg)
    return out


__all__ = [
    "DEFAULT_CONCURRENT",
    "DEFAULT_RPM",
    "MCQ_SYSTEM_PROMPT",
    "MCQ_USER_TEMPLATE",
    "ModelAggregate",
    "ModelItemResult",
    "aggregate_results",
    "evaluate_model_async",
    "load_filtered_items",
    "parse_correct_option",
    "parse_mcq_answer",
    "run_native_mcq_async",
    "score_item",
    "write_report",
]


# 默认入参（在不进 eval CLI 的情况下也能 `python -m eval.scripts.native_mcq_runner` 跑）
DEFAULT_INPUT = (
    Path(__file__).resolve().parents[1] / "teleqna" / "data" / "filtered" / "filtered.jsonl"
)
DEFAULT_OUTPUT_BASE = Path(__file__).resolve().parents[2] / "eval-results" / "m7-native-mcq"


def main(
    *,
    input_path: Path = DEFAULT_INPUT,
    out_base: Path = DEFAULT_OUTPUT_BASE,
    models: list[str] | None = None,
    limit: int = 0,
    rpm: int = DEFAULT_RPM,
    concurrent: int = DEFAULT_CONCURRENT,
) -> Path:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    s = get_settings()
    models = models or [s.llm_light_model, s.llm_judge_model]  # mimo-v2.5, glm-5.1
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = out_base / ts
    return asyncio.run(
        run_native_mcq_async(
            input_path=input_path,
            out_dir=out_dir,
            models=models,
            limit=limit,
            rpm=rpm,
            concurrent=concurrent,
            settings=s,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
