"""T2 转化：LLM (mimo-v2.5-pro) 把 MCQ → 开放问答 + 期望事实 + spec 章节。

设计：
- 复用 teleqna/infer.py 的 _LiteLLMChatClient / _RpmLimiter（这两个是通用 async LLM 调用工具）
- 严格 JSON 解析 + whitelist 后处理
- 输出：v1.draft.yaml（按 06-...md §3.5 schema）+ failed.jsonl + skipped.jsonl
- skip 条件：LLM 标 skip_reason / expected_specs 全部 ∉ whitelist / facts < 3 个

不做：
- 人审 CLI（review.py 占位，按 M3 阶段实际候选量决定要不要写）
- 失败 retry 之外的"自动修复"（让人审兜底）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from eval.settings import EvalSettings, get_settings
from eval.teleqna.infer import (
    DEFAULT_TIMEOUT_S,
    _extract_json,
    _LiteLLMChatClient,
    _RpmLimiter,
)
from eval.teleqna.prompts import options_from_item
from eval.teleqna.whitelist import POC_17_SPECS

from .prompts import VALID_CATEGORIES, build_transform_messages

log = logging.getLogger(__name__)

DEFAULT_TRANSFORM_MAX_TOKENS = 8192  # 同 ingestion vision；reasoning 模型必须给足
DEFAULT_RPM = 50  # mimo-v2.5-pro 也是 100 RPM 上限；留余量
DEFAULT_CONCURRENT = 10

MIN_FACTS = 3
MAX_FACTS = 7
MAX_FORBIDDEN = 3
MAX_QUESTION_CHARS = 1000


class TransformError(Exception):
    """T2 转化对外异常基类。"""


@dataclass(slots=True)
class TransformResult:
    """单题转化结果。"""

    item_id: str
    accepted: bool
    skip_reason: str | None = None
    error: str | None = None
    yaml_item: dict | None = None
    raw_response: str | None = None
    elapsed_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class TransformStats:
    total: int = 0
    accepted: int = 0
    skipped: int = 0
    failed: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_spec: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    prompt_tokens_total: int = 0
    completion_tokens_total: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "skipped": self.skipped,
            "failed": self.failed,
            "by_category": dict(sorted(self.by_category.items())),
            "by_spec": dict(sorted(self.by_spec.items())),
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
            "elapsed_s": round(self.elapsed_s, 1),
            "prompt_tokens_total": self.prompt_tokens_total,
            "completion_tokens_total": self.completion_tokens_total,
        }


def _validate_and_normalize(parsed: dict[str, Any]) -> tuple[dict | None, str | None]:
    """LLM 返回的 JSON → 规范化 v1.yaml item dict 或 (None, skip_reason)。

    返回 (item, skip_reason)：
      - item != None, skip_reason == None → 接受
      - item == None, skip_reason != None → 跳过（LLM 主动 skip 或 后处理 reject）
    """
    skip_reason = parsed.get("skip_reason")
    if skip_reason:
        return None, str(skip_reason)[:200]

    q = str(parsed.get("rewritten_question") or "").strip()
    if not q:
        return None, "empty-rewritten-question"
    if len(q) > MAX_QUESTION_CHARS:
        q = q[:MAX_QUESTION_CHARS] + "…"

    category = str(parsed.get("category") or "").strip().lower()
    if category not in VALID_CATEGORIES:
        return None, f"invalid-category: {category}"

    raw_specs = parsed.get("expected_specs") or []
    if not isinstance(raw_specs, list):
        return None, "expected_specs-not-list"

    expected_specs: list[dict] = []
    seen: set[str] = set()
    for s in raw_specs:
        if not isinstance(s, dict):
            continue
        spec_id_raw = str(s.get("spec_id") or "").strip()
        # 容错：可能有 "TS 38.331"
        spec_id = spec_id_raw.split()[-1].split("-")[0] if spec_id_raw else ""
        if spec_id not in POC_17_SPECS:
            continue
        if spec_id in seen:
            continue
        seen.add(spec_id)
        sections_raw = s.get("sections") or []
        if isinstance(sections_raw, str):
            sections_raw = [sections_raw]
        sections = [str(x) for x in sections_raw if str(x).strip()]
        expected_specs.append({"spec_id": spec_id, "sections": sections})

    if not expected_specs and category != "negative":
        return None, "no-whitelist-spec-and-not-negative"

    facts_raw = parsed.get("expected_facts") or []
    if not isinstance(facts_raw, list):
        return None, "expected_facts-not-list"
    facts = [str(f).strip() for f in facts_raw if str(f).strip()]
    facts = [f if len(f) <= 200 else f[:200] for f in facts]
    if category != "negative" and len(facts) < MIN_FACTS:
        return None, f"facts<{MIN_FACTS}"
    if len(facts) > MAX_FACTS:
        facts = facts[:MAX_FACTS]

    forbidden_raw = parsed.get("forbidden") or []
    if not isinstance(forbidden_raw, list):
        forbidden_raw = []
    forbidden = [str(f).strip() for f in forbidden_raw if str(f).strip()][:MAX_FORBIDDEN]

    must_say_not_found = bool(parsed.get("must_say_not_found"))
    if category == "negative":
        must_say_not_found = True  # 强制

    language = str(parsed.get("language") or "en").lower()
    if language not in {"en", "zh"}:
        language = "en"

    notes = str(parsed.get("notes") or "").strip()
    if len(notes) > 300:
        notes = notes[:300]

    item: dict = {
        "category": category,
        "language": language,
        "question": q,
        "expected_specs": expected_specs,
        "expected_facts": facts,
        "forbidden": forbidden,
    }
    if must_say_not_found:
        item["must_say_not_found"] = True
    if notes:
        item["notes"] = notes
    return item, None


def _assign_item_id(category: str, idx: int) -> str:
    """e.g. def-001, proc-005，与 06-...md §3.5 示例一致。"""
    short = {
        "definition": "def",
        "procedure": "proc",
        "multi_section": "multi",
        "table_lookup": "table",
        "formula": "form",
        "tool": "tool",
        "negative": "neg",
    }.get(category, "qa")
    return f"{short}-{idx:03d}"


async def _transform_one(
    candidate: dict,
    *,
    client: _LiteLLMChatClient,
    limiter: _RpmLimiter,
    semaphore: asyncio.Semaphore,
    max_tokens: int = DEFAULT_TRANSFORM_MAX_TOKENS,
    max_retries: int = 2,
) -> TransformResult:
    """对单个候选 raw item 跑 LLM 转化。"""
    item_id = str(candidate.get("id") or "")
    inferred = candidate.get("llm_in_whitelist") or candidate.get("expected_specs_inferred") or []
    messages = build_transform_messages(
        question=str(candidate.get("question", "")),
        options=options_from_item(candidate),
        answer=str(candidate.get("answer", "")),
        explanation=str(candidate.get("explanation", "") or ""),
        inferred_specs=[str(s) for s in inferred],
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
                    payload = await client.chat(messages=messages, max_tokens=max_tokens)
        except RetryError as exc:
            last = exc.last_attempt.exception()
            return TransformResult(
                item_id=item_id,
                accepted=False,
                error=f"http retries exhausted: {type(last).__name__}: {last}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
            return TransformResult(
                item_id=item_id,
                accepted=False,
                error=f"http error: {type(exc).__name__}: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        choice = (payload.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        elapsed_ms = (time.perf_counter() - t0) * 1000

        try:
            parsed = _extract_json(content)
        except Exception as exc:
            return TransformResult(
                item_id=item_id,
                accepted=False,
                error=f"json parse: {exc}",
                raw_response=content,
                elapsed_ms=elapsed_ms,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            )

        item, skip_reason = _validate_and_normalize(parsed)
        if item is None:
            return TransformResult(
                item_id=item_id,
                accepted=False,
                skip_reason=skip_reason,
                raw_response=content,
                elapsed_ms=elapsed_ms,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            )

        # 加 source + teleqna_origin_id（§3.5 schema 必填）
        item["source"] = "teleqna_transformed"
        item["teleqna_origin_id"] = item_id
        return TransformResult(
            item_id=item_id,
            accepted=True,
            yaml_item=item,
            raw_response=content,
            elapsed_ms=elapsed_ms,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


def _write_yaml_draft(
    accepted_items: list[dict],
    out_path: Path,
    *,
    version: int = 1,
) -> None:
    """写 v1.draft.yaml，结构与 06-...md §3.5 对齐。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cats_counter: dict[str, int] = {}
    # 给每个 item 分配 id（先按 category 排序保持稳定）
    accepted_items = sorted(
        accepted_items, key=lambda x: (x["category"], x.get("teleqna_origin_id", ""))
    )
    for item in accepted_items:
        cats_counter[item["category"]] = cats_counter.get(item["category"], 0) + 1
        item["id"] = _assign_item_id(item["category"], cats_counter[item["category"]])

    doc = {
        "version": version,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "total": len(accepted_items),
        "sources": ["teleqna_transformed"],
        "categories": sorted({i["category"] for i in accepted_items}),
        "items": accepted_items,
    }
    out_path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120),
        encoding="utf-8",
    )
    log.info("wrote draft golden YAML: %s (n=%d)", out_path, len(accepted_items))


async def transform_batch_async(
    candidates: Iterable[dict],
    *,
    out_yaml: Path,
    skipped_path: Path | None = None,
    failed_path: Path | None = None,
    client: _LiteLLMChatClient,
    max_tokens: int = DEFAULT_TRANSFORM_MAX_TOKENS,
    rpm: int = DEFAULT_RPM,
    concurrent: int = DEFAULT_CONCURRENT,
    progress_every: int = 25,
) -> TransformStats:
    """LLM 转化 + 写 v1.draft.yaml + skipped/failed jsonl + stats。"""
    out_yaml.parent.mkdir(parents=True, exist_ok=True)
    skipped_path = skipped_path or (out_yaml.parent / "v1.skipped.jsonl")
    failed_path = failed_path or (out_yaml.parent / "v1.failed.jsonl")
    limiter = _RpmLimiter(rpm)
    sem = asyncio.Semaphore(int(concurrent))

    stats = TransformStats()
    t_all = time.perf_counter()
    candidates_list = list(candidates)
    log.info(
        "transform start: total=%d rpm=%d concurrent=%d model=%s max_tokens=%d",
        len(candidates_list),
        rpm,
        concurrent,
        client.model,
        max_tokens,
    )

    accepted_items: list[dict] = []
    skipped_f = skipped_path.open("w", encoding="utf-8")
    failed_f = failed_path.open("w", encoding="utf-8")

    try:

        async def _wrapped(cand: dict) -> tuple[dict, TransformResult]:
            r = await _transform_one(
                cand,
                client=client,
                limiter=limiter,
                semaphore=sem,
                max_tokens=max_tokens,
            )
            return cand, r

        tasks = [asyncio.create_task(_wrapped(c)) for c in candidates_list]
        for done_idx, fut in enumerate(asyncio.as_completed(tasks), start=1):
            cand, result = await fut
            stats.total += 1
            stats.prompt_tokens_total += result.prompt_tokens
            stats.completion_tokens_total += result.completion_tokens

            if result.accepted and result.yaml_item is not None:
                stats.accepted += 1
                accepted_items.append(result.yaml_item)
                stats.by_category[result.yaml_item["category"]] = (
                    stats.by_category.get(result.yaml_item["category"], 0) + 1
                )
                for sp in result.yaml_item.get("expected_specs", []):
                    sid = sp.get("spec_id")
                    if sid:
                        stats.by_spec[sid] = stats.by_spec.get(sid, 0) + 1
            elif result.error:
                stats.failed += 1
                failed_f.write(
                    json.dumps(
                        {
                            "item_id": result.item_id,
                            "error": result.error,
                            "raw_response": result.raw_response,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            else:
                stats.skipped += 1
                reason = result.skip_reason or "unknown"
                stats.skip_reasons[reason] = stats.skip_reasons.get(reason, 0) + 1
                skipped_f.write(
                    json.dumps(
                        {
                            "item_id": result.item_id,
                            "skip_reason": reason,
                            "question": str(cand.get("question", ""))[:200],
                            "raw_response": result.raw_response,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            if done_idx % progress_every == 0:
                skipped_f.flush()
                failed_f.flush()
                elapsed = time.perf_counter() - t_all
                rps = done_idx / max(elapsed, 1e-6)
                log.info(
                    "transform progress: %d / %d (%.1f rps) | " "accepted=%d skipped=%d failed=%d",
                    done_idx,
                    len(candidates_list),
                    rps,
                    stats.accepted,
                    stats.skipped,
                    stats.failed,
                )
    finally:
        skipped_f.close()
        failed_f.close()

    _write_yaml_draft(accepted_items, out_yaml)
    stats.elapsed_s = time.perf_counter() - t_all
    log.info("transform done: %s", stats.to_dict())
    return stats


def build_transform_client(settings: EvalSettings | None = None) -> _LiteLLMChatClient:
    """mimo-v2.5-pro client（D3 决议：转化用 pro）。"""
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise TransformError("LITELLM_API_KEY missing in env / .env")
    return _LiteLLMChatClient(
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        model=s.llm_agent_model,  # mimo-v2.5-pro
        timeout_s=DEFAULT_TIMEOUT_S,
    )


__all__ = [
    "DEFAULT_CONCURRENT",
    "DEFAULT_RPM",
    "DEFAULT_TRANSFORM_MAX_TOKENS",
    "MAX_FACTS",
    "MAX_FORBIDDEN",
    "MIN_FACTS",
    "TransformError",
    "TransformResult",
    "TransformStats",
    "_assign_item_id",
    "_validate_and_normalize",
    "build_transform_client",
    "transform_batch_async",
]
