"""eval/teleqna/infer.py 单测（不打 LLM）。

覆盖：
- _extract_json：fence / 前置文本 / 直接 JSON / 嵌套 / 异常
- _normalize_inferred_specs：whitelist 过滤 / "TS XX.XXX" 容错 / 去重
- _RpmLimiter：受限制能让 N 次调用至少花 N*period 秒
- infer_batch_async：fake client → 全 happy / 部分失败 / out_of_scope
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from eval.teleqna.infer import (
    InferStats,
    _extract_json,
    _LiteLLMChatClient,
    _normalize_inferred_specs,
    _RpmLimiter,
    infer_batch_async,
)


class TestExtractJson:
    def test_plain_json(self) -> None:
        d = _extract_json('{"expected_specs": ["38.331"], "confidence": "high"}')
        assert d["expected_specs"] == ["38.331"]

    def test_fenced_json(self) -> None:
        d = _extract_json(
            'Sure, here is the result:\n```json\n{"expected_specs": ["23.501"], '
            '"confidence": "medium"}\n```'
        )
        assert d["expected_specs"] == ["23.501"]
        assert d["confidence"] == "medium"

    def test_fenced_no_json_label(self) -> None:
        d = _extract_json('```\n{"expected_specs": []}\n```')
        assert d["expected_specs"] == []

    def test_preamble_then_json(self) -> None:
        d = _extract_json(
            "Based on the question, the spec is 38.331.\n\n"
            '{"expected_specs": ["38.331"], "confidence": "high", '
            '"rationale": "RRC", "out_of_scope_reason": null}'
        )
        assert d["expected_specs"] == ["38.331"]
        assert d["out_of_scope_reason"] is None

    def test_empty_response(self) -> None:
        with pytest.raises(Exception, match="empty LLM response"):
            _extract_json("")

    def test_no_json(self) -> None:
        with pytest.raises(Exception, match="no JSON found"):
            _extract_json("the spec is 38.331 and that's it")

    def test_invalid_json(self) -> None:
        with pytest.raises(Exception, match="JSON decode error"):
            _extract_json("{not valid json at all,,,}")


class TestNormalizeInferredSpecs:
    def test_keeps_only_whitelist(self) -> None:
        # 23.501 / 38.331 / 24.501 在 whitelist；22.011 / 38.213 不在
        result = _normalize_inferred_specs(["23.501", "22.011", "38.213", "38.331"])
        assert result == ["23.501", "38.331"]

    def test_dedupe(self) -> None:
        result = _normalize_inferred_specs(["38.331", "38.331", "23.501", "23.501"])
        assert result == ["38.331", "23.501"]

    def test_strips_ts_prefix(self) -> None:
        # "TS 38.331" → 取最后一段 → 38.331
        result = _normalize_inferred_specs(["TS 38.331", "TS 23.501"])
        assert result == ["38.331", "23.501"]

    def test_strips_version_suffix(self) -> None:
        result = _normalize_inferred_specs(["38.331-h60", "23.501-h70"])
        assert result == ["38.331", "23.501"]

    def test_invalid_input(self) -> None:
        assert _normalize_inferred_specs([]) == []
        assert _normalize_inferred_specs("not a list") == []  # type: ignore[arg-type]
        assert _normalize_inferred_specs(["foo", "12"]) == []

    def test_drops_outside_whitelist_silently(self) -> None:
        # 完全不在 whitelist → 空列表（caller 会写 out_of_scope）
        assert _normalize_inferred_specs(["22.011", "38.213"]) == []


class TestRpmLimiter:
    @pytest.mark.asyncio
    async def test_throttles_to_rpm(self) -> None:
        # rpm=120 → period 0.5s；3 次 acquire 至少 1.0s（period * 2，第一次免等）
        limiter = _RpmLimiter(rpm=120)
        t0 = time.perf_counter()
        for _ in range(3):
            await limiter.acquire()
        elapsed = time.perf_counter() - t0
        assert elapsed >= 1.0, f"expected >= 1.0s, got {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_minimum_rpm_one(self) -> None:
        # rpm=0 应被夹到 1
        limiter = _RpmLimiter(rpm=0)
        assert limiter.rpm == 1


# ---------- infer_batch_async fake client ----------


class _FakeChatClient(_LiteLLMChatClient):
    """绕过真实 httpx；按 item.id 返回构造的 response。"""

    def __init__(self, responses_by_id: dict[str, Any]) -> None:
        # 不调 super().__init__ 避免起 httpx
        self._responses = responses_by_id
        self.model = "fake-mimo"
        self.calls: list[str] = []

    async def aclose(self) -> None:
        pass

    async def chat(  # type: ignore[override]
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        # 从 user message 里抽 question 段，匹配 fixture
        user = next(m["content"] for m in messages if m["role"] == "user")
        first_line = user.splitlines()[0]
        # "Question: <text>" → text 第一段做 key（测试 fixture 用 q1/q2/q3）
        key = first_line.split("Question:", 1)[1].strip().split()[0]
        self.calls.append(key)
        if key not in self._responses:
            raise RuntimeError(f"no fake response for key={key}")
        resp = self._responses[key]
        if isinstance(resp, Exception):
            raise resp
        return resp


def _mk_item(id_: str, question: str, *, answer: str = "option 1") -> dict:
    return {
        "id": id_,
        "question": question,
        "option 1": "A",
        "option 2": "B",
        "answer": answer,
        "explanation": "no spec ref here",
        "category": "Standards specifications",
    }


def _ok_response(specs: list[str], conf: str = "high") -> dict:
    body = {
        "expected_specs": specs,
        "confidence": conf,
        "rationale": "test",
        "out_of_scope_reason": None if specs else "no-clear-spec",
    }
    return {
        "choices": [{"message": {"content": json.dumps(body)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


class TestInferBatchAsync:
    @pytest.mark.asyncio
    async def test_happy_path_writes_jsonl_and_stats(self, tmp_path: Path) -> None:
        items = [
            _mk_item("q1", "q1 about AMF"),
            _mk_item("q2", "q2 about RRC"),
            _mk_item("q3", "q3 about IEEE 802.11"),
        ]
        responses = {
            "q1": _ok_response(["23.501"]),
            "q2": _ok_response(["38.331"]),
            "q3": _ok_response([], conf="high"),
        }
        client = _FakeChatClient(responses)

        out = tmp_path / "infer.jsonl"
        stats = await infer_batch_async(
            items,
            out_path=out,
            client=client,  # type: ignore[arg-type]
            rpm=600,
            concurrent=4,
            progress_every=10,
        )

        assert stats.total == 3
        assert stats.succeeded == 3
        assert stats.failed == 0
        assert stats.in_whitelist == 2
        assert stats.out_of_scope == 1
        assert stats.by_spec == {"23.501": 1, "38.331": 1}
        assert stats.prompt_tokens_total == 300
        assert stats.completion_tokens_total == 150

        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 3
        # 每行都有 LLM 字段
        for r in rows:
            assert "llm_in_whitelist" in r
            assert "llm_confidence" in r
            assert "llm_elapsed_ms" in r

    @pytest.mark.asyncio
    async def test_partial_failure_writes_failed_jsonl(self, tmp_path: Path) -> None:
        items = [_mk_item("q1", "q1"), _mk_item("q2", "q2")]
        responses: dict[str, Any] = {
            "q1": _ok_response(["38.331"]),
            "q2": {  # 故意非 JSON content → JSON decode fail
                "choices": [{"message": {"content": "no json here just talk"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        }
        client = _FakeChatClient(responses)

        out = tmp_path / "infer.jsonl"
        stats = await infer_batch_async(
            items, out_path=out, client=client, rpm=600, concurrent=2  # type: ignore[arg-type]
        )

        assert stats.total == 2
        assert stats.succeeded == 1
        assert stats.failed == 1
        assert stats.in_whitelist == 1

        failed = (out.parent / (out.stem + ".failed.jsonl")).read_text().splitlines()
        assert len(failed) == 1
        failed_row = json.loads(failed[0])
        assert failed_row["item"]["id"] == "q2"
        assert "no JSON found" in failed_row["error"]

    @pytest.mark.asyncio
    async def test_out_of_scope_specs_dropped(self, tmp_path: Path) -> None:
        items = [_mk_item("q1", "q1")]
        # LLM 错误地输出了非 whitelist 的 spec，应被 _normalize 过滤掉
        responses = {"q1": _ok_response(["22.011", "38.213"])}
        client = _FakeChatClient(responses)
        out = tmp_path / "infer.jsonl"
        stats = await infer_batch_async(
            items, out_path=out, client=client, rpm=600, concurrent=1  # type: ignore[arg-type]
        )
        assert stats.in_whitelist == 0
        assert stats.out_of_scope == 1


class TestInferStats:
    def test_to_dict(self) -> None:
        s = InferStats(total=10, succeeded=8, failed=2, in_whitelist=5, out_of_scope=3)
        s.by_spec = {"38.331": 3, "23.501": 2}
        s.by_confidence = {"high": 4, "medium": 4}
        s.elapsed_s = 12.34
        d = s.to_dict()
        assert d["total"] == 10
        assert d["by_spec"] == {"23.501": 2, "38.331": 3}  # sorted
        assert d["elapsed_s"] == 12.3


def test_iterable_param_compat() -> None:
    """sanity: infer_batch_async signature accepts Iterable not just list."""
    from inspect import signature

    sig = signature(infer_batch_async)
    assert "items" in sig.parameters

    # silence unused import
    _ = Iterable
    _ = asyncio
