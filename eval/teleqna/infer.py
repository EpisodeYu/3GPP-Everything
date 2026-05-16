"""LLM 辅助 spec 推断（mimo-v2.5）。

输入：raw.jsonl 中"category 命中且 explanation/options 没明确引 spec_id"的那批
   （以及 out_of_scope.jsonl 也可二轮跑——可能 spec 引用形式诡异）
输出：llm_inferred.jsonl，逐题加 expected_specs / confidence / rationale 字段

工程要点：
- 异步并发（默认 8）+ RPM 限速（默认 60，留 mimo 100 RPM 上限的 60% 给其他任务）
- 严格 JSON 解析，宽容兜底（mimo 偶尔加 markdown fences / leading explanation）
- 失败容错：单题 retry 2 次仍失败 → 写 failed.jsonl，不阻塞整体
- 进度持久化：每 100 题 flush 一次
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
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

from .prompts import build_spec_infer_messages, options_from_item
from .whitelist import POC_17_SPECS

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 120.0
# mimo-v2.5 是 reasoning 模型，所有 max_tokens 优先给 reasoning chain；
# 评测过 ingestion/images/vision.py 同模型默认 8192，给小了 (e.g. 512) 会导致 content="" 全失败。
# 实际答案 JSON 通常 <200 tokens；8192 留足 reasoning 预算 + 不计费多余 (按 actual completion 计)。
DEFAULT_MAX_TOKENS = 8192
DEFAULT_RPM = 60  # 留余量给其他任务（mimo 限速 100 RPM）
DEFAULT_CONCURRENT = 8


class InferError(Exception):
    """LLM 推断对外异常基类。"""


@dataclass(slots=True)
class InferResult:
    """单题 LLM 推断结果。"""

    item_id: str
    expected_specs: list[str]
    confidence: str
    rationale: str
    out_of_scope_reason: str | None = None
    raw_response: str | None = None
    in_whitelist: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


# 鲁棒 JSON 提取：mimo 偶尔输出 ```json\n{...}\n``` 或前置一句解释
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 响应里抽出 JSON object。

    优先级：
      1. ```json``` fence 内
      2. 第一个 `{...}` block（贪婪匹配，让 inner string 含 `}` 也能读全）
    """
    if not text:
        raise InferError("empty LLM response")
    # 1. fenced
    m = _JSON_FENCE_RE.search(text)
    if m:
        candidate = m.group(1)
    else:
        # 2. greedy block
        m = _JSON_OBJECT_RE.search(text)
        if not m:
            raise InferError(f"no JSON found in response: {text[:200]}")
        candidate = m.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise InferError(f"JSON decode error: {exc}; candidate={candidate[:300]}") from exc


def _normalize_inferred_specs(raw: list[Any]) -> list[str]:
    """LLM 输出的 expected_specs 列表 → 去重 / 字符串化 / 仅保留命中 17 篇 whitelist。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        s = str(x).strip()
        # 容错：模型可能输出 "TS 23.501" / "23.501-h60"
        if " " in s:
            s = s.split()[-1]
        s = s.split("-")[0]
        if s in POC_17_SPECS and s not in seen:
            seen.add(s)
            out.append(s)
    return out


class _LiteLLMChatClient:
    """async chat completions 薄封装。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._owns = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def chat(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        resp.raise_for_status()
        return resp.json()


class _RpmLimiter:
    """简单 RPM token bucket（asyncio）。

    每秒补充 rpm/60 个 token；request 前 await 一个 token。
    精度对评测任务足够（rpm=60 → 一个 token 1.0s）。
    """

    def __init__(self, rpm: int) -> None:
        self.rpm = max(int(rpm), 1)
        self._period = 60.0 / self.rpm
        self._lock = asyncio.Lock()
        self._next_at = time.monotonic()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            # 下一个 slot 至少在 _next_at + period 之后
            self._next_at = max(now, self._next_at) + self._period


@dataclass(slots=True)
class InferStats:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    in_whitelist: int = 0
    out_of_scope: int = 0
    by_spec: dict[str, int] = field(default_factory=dict)
    by_confidence: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    prompt_tokens_total: int = 0
    completion_tokens_total: int = 0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "in_whitelist": self.in_whitelist,
            "out_of_scope": self.out_of_scope,
            "by_spec": dict(sorted(self.by_spec.items())),
            "by_confidence": dict(self.by_confidence),
            "elapsed_s": round(self.elapsed_s, 1),
            "prompt_tokens_total": self.prompt_tokens_total,
            "completion_tokens_total": self.completion_tokens_total,
        }


async def _infer_one(
    item: dict,
    *,
    client: _LiteLLMChatClient,
    limiter: _RpmLimiter,
    semaphore: asyncio.Semaphore,
    max_retries: int = 2,
) -> InferResult:
    """对单题 LLM 推断。失败 → InferResult.error。"""
    item_id = str(item.get("id") or "")
    options = options_from_item(item)
    messages = build_spec_infer_messages(
        question=str(item.get("question", "")),
        options=options,
        answer=str(item.get("answer", "")),
        explanation=str(item.get("explanation", "") or ""),
    )

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
                    payload = await client.chat(messages=messages)
        except RetryError as exc:
            last = exc.last_attempt.exception()
            return InferResult(
                item_id=item_id,
                expected_specs=[],
                confidence="low",
                rationale="",
                error=f"http retries exhausted: {type(last).__name__}: {last}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
            return InferResult(
                item_id=item_id,
                expected_specs=[],
                confidence="low",
                rationale="",
                error=f"http error: {type(exc).__name__}: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        choice = (payload.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        elapsed_ms = (time.perf_counter() - t0) * 1000

        try:
            parsed = _extract_json(content)
        except InferError as exc:
            return InferResult(
                item_id=item_id,
                expected_specs=[],
                confidence="low",
                rationale="",
                error=str(exc),
                raw_response=content,
                elapsed_ms=elapsed_ms,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            )

        raw_specs = parsed.get("expected_specs") or []
        in_white = _normalize_inferred_specs(raw_specs)
        return InferResult(
            item_id=item_id,
            expected_specs=[str(x) for x in (raw_specs if isinstance(raw_specs, list) else [])],
            in_whitelist=in_white,
            confidence=str(parsed.get("confidence") or "low"),
            rationale=str(parsed.get("rationale") or "")[:500],
            out_of_scope_reason=(
                str(parsed.get("out_of_scope_reason"))
                if parsed.get("out_of_scope_reason") is not None
                else None
            ),
            raw_response=content,
            elapsed_ms=elapsed_ms,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


async def infer_batch_async(
    items: Iterable[dict],
    *,
    out_path: Path,
    failed_path: Path | None = None,
    client: _LiteLLMChatClient,
    rpm: int = DEFAULT_RPM,
    concurrent: int = DEFAULT_CONCURRENT,
    progress_every: int = 50,
    max_retries: int = 2,
) -> InferStats:
    """对 items 流跑 LLM 推断 → 逐题 jsonl。返回聚合 stats。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if failed_path is None:
        failed_path = out_path.parent / (out_path.stem + ".failed.jsonl")
    limiter = _RpmLimiter(rpm)
    sem = asyncio.Semaphore(int(concurrent))

    stats = InferStats()
    t_all = time.perf_counter()

    items_list = list(items)
    total_target = len(items_list)
    log.info(
        "llm_infer start: total=%d rpm=%d concurrent=%d model=%s",
        total_target,
        rpm,
        concurrent,
        client.model,
    )

    out_f = out_path.open("w", encoding="utf-8")
    failed_f = failed_path.open("w", encoding="utf-8")
    try:

        async def _wrapped(item: dict) -> tuple[dict, InferResult]:
            r = await _infer_one(
                item,
                client=client,
                limiter=limiter,
                semaphore=sem,
                max_retries=max_retries,
            )
            return item, r

        # 用 as_completed 实时 flush
        tasks = [asyncio.create_task(_wrapped(it)) for it in items_list]
        for done_idx, fut in enumerate(asyncio.as_completed(tasks), start=1):
            item, result = await fut
            stats.total += 1
            stats.prompt_tokens_total += result.prompt_tokens
            stats.completion_tokens_total += result.completion_tokens

            if result.error:
                stats.failed += 1
                failed_f.write(
                    json.dumps(
                        {"item": item, "error": result.error, "raw_response": result.raw_response},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            else:
                stats.succeeded += 1
                stats.by_confidence[result.confidence] = (
                    stats.by_confidence.get(result.confidence, 0) + 1
                )
                if result.in_whitelist:
                    stats.in_whitelist += 1
                    for s in result.in_whitelist:
                        stats.by_spec[s] = stats.by_spec.get(s, 0) + 1
                else:
                    stats.out_of_scope += 1
                # 合并到原 item 写出
                merged = dict(item)
                merged["llm_expected_specs"] = result.expected_specs
                merged["llm_in_whitelist"] = result.in_whitelist
                merged["llm_confidence"] = result.confidence
                merged["llm_rationale"] = result.rationale
                merged["llm_out_of_scope_reason"] = result.out_of_scope_reason
                merged["llm_elapsed_ms"] = round(result.elapsed_ms, 1)
                out_f.write(json.dumps(merged, ensure_ascii=False) + "\n")

            if done_idx % progress_every == 0:
                out_f.flush()
                failed_f.flush()
                elapsed = time.perf_counter() - t_all
                rps = done_idx / max(elapsed, 1e-6)
                log.info(
                    "progress: %d / %d (%.1f rps) | succeeded=%d failed=%d "
                    "in_whitelist=%d out_of_scope=%d",
                    done_idx,
                    total_target,
                    rps,
                    stats.succeeded,
                    stats.failed,
                    stats.in_whitelist,
                    stats.out_of_scope,
                )
    finally:
        out_f.close()
        failed_f.close()

    stats.elapsed_s = time.perf_counter() - t_all
    log.info("llm_infer done: %s", stats.to_dict())
    return stats


def build_default_client(settings: EvalSettings | None = None) -> _LiteLLMChatClient:
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise InferError("LITELLM_API_KEY missing in env / .env")
    return _LiteLLMChatClient(
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        model=s.llm_light_model,  # mimo-v2.5
    )


__all__ = [
    "DEFAULT_CONCURRENT",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_RPM",
    "DEFAULT_TIMEOUT_S",
    "InferError",
    "InferResult",
    "InferStats",
    "_extract_json",
    "_normalize_inferred_specs",
    "build_default_client",
    "infer_batch_async",
]
