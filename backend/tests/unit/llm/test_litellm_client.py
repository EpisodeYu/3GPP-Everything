"""LiteLLMClient 行为测试（mock transport）。

不连真实 LiteLLM；通过 httpx.MockTransport 注入响应。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import LLMError
from app.llm.litellm_client import LiteLLMClient


class _CapturedObservation:
    def __init__(self) -> None:
        self.active = True
        self.start: dict[str, Any] = {}
        self.successes: list[dict[str, Any]] = []
        self.errors: list[tuple[BaseException, Any]] = []

    def finish_success(self, **kwargs: Any) -> None:
        self.successes.append(kwargs)

    def finish_error(self, exc: BaseException, *, telemetry: Any) -> None:
        self.errors.append((exc, telemetry))


def _capture_observation(monkeypatch: Any) -> _CapturedObservation:
    import app.llm.litellm_client as client_mod

    captured = _CapturedObservation()

    def _start(**kwargs: Any) -> _CapturedObservation:
        captured.start = kwargs
        return captured

    monkeypatch.setattr(client_mod.LangfuseObservation, "start", staticmethod(_start))
    return captured


def _settings(**over: Any) -> Settings:
    defaults: dict[str, Any] = dict(
        LITELLM_BASE_URL="http://test/v1",
        LITELLM_API_KEY="sk-test",
        VOYAGE_EMBEDDING_MODEL="voyage-4-large",
        VOYAGE_RERANK_MODEL="rerank-2.5",
        EMBEDDING_DIMENSIONS=1024,
    )
    defaults.update(over)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_chat_success() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers.get("authorization")
        captured["request_id"] = req.headers.get("x-request-id")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        resp = await cli.chat(messages=[{"role": "user", "content": "hi"}], model="mimo-v2.5")

    assert resp["choices"][0]["message"]["content"] == "ok"
    assert captured["url"] == "http://test/v1/chat/completions"
    assert captured["body"]["model"] == "mimo-v2.5"
    assert captured["body"]["stream"] is False
    assert captured["auth"] == "Bearer sk-test"
    assert len(captured["request_id"]) == 32


async def test_chat_observation_records_usage_cost_and_masked_io(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "private answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        response = await cli.chat(
            messages=[{"role": "user", "content": "private question"}],
            model="mimo-v2.5",
            temperature=0.2,
        )

    assert response["choices"][0]["message"]["content"] == "private answer"
    assert observation.start["name"] == "litellm.chat"
    assert observation.start["as_type"] == "generation"
    assert observation.start["input"] == {
        "message_count": 1,
        "roles": {"user": 1},
        "content_chars": 16,
    }
    success = observation.successes[0]
    assert success["output"] == {
        "choice_count": 1,
        "content_chars": 14,
        "finish_reasons": ["stop"],
    }
    assert success["usage_details"] == {"input": 10, "output": 3, "total": 13}
    assert success["cost_details"] == pytest.approx(
        {
            "input": 0.000004,
            "output": 0.000006,
            "total": 0.00001,
        }
    )
    assert success["telemetry"].status_code == 200


async def test_chat_thinking_uses_gateway_reasoning_contract() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        await cli.chat(
            messages=[{"role": "user", "content": "x"}],
            model="mimo-v2.5",
            thinking={"type": "disabled"},
        )

    body = captured["body"]
    assert "thinking" not in body
    assert "extra_body" not in body
    assert body["reasoning_control"] == "disabled"


async def test_chat_4xx_raises_llm_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        with pytest.raises(LLMError) as ei:
            await cli.chat(messages=[{"role": "user", "content": "x"}])
    assert "HTTP 400" in ei.value.message


async def test_chat_error_observation_records_4xx_without_body(
    monkeypatch: Any,
) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "private upstream body"}})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        with pytest.raises(LLMError):
            await cli.chat(messages=[{"role": "user", "content": "x"}])

    error, telemetry = observation.errors[0]
    assert isinstance(error, LLMError)
    assert telemetry.status_code == 429
    assert telemetry.retry_count == 0
    assert "private upstream body" not in repr(observation.errors)


async def test_chat_5xx_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, json={"error": "transient"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with LiteLLMClient(
        settings=_settings(), client=_mock_client(handler), max_retries=3
    ) as cli:
        resp = await cli.chat(messages=[{"role": "user", "content": "x"}])

    assert resp["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 2


async def test_chat_observation_reports_retry_count(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    async with LiteLLMClient(
        settings=_settings(), client=_mock_client(handler), max_retries=1
    ) as cli:
        await cli.chat(messages=[{"role": "user", "content": "x"}])

    telemetry = observation.successes[0]["telemetry"]
    assert telemetry.status_code == 200
    assert telemetry.retry_count == 1


async def test_chat_timeout_keeps_exception_and_finishes_observation(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=req)

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        with pytest.raises(httpx.ReadTimeout):
            await cli.chat(messages=[{"role": "user", "content": "x"}])

    error, telemetry = observation.errors[0]
    assert isinstance(error, httpx.ReadTimeout)
    assert telemetry.status_code is None


async def test_chat_default_leaves_retry_ownership_to_gateway() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": "transient"})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        with pytest.raises(httpx.HTTPStatusError):
            await cli.chat(messages=[{"role": "user", "content": "x"}])

    assert calls["n"] == 1


async def test_chat_rejects_invalid_reasoning_control() -> None:
    async with LiteLLMClient(settings=_settings(), client=_mock_client(lambda _: None)) as cli:
        with pytest.raises(ValueError, match=r"thinking\.type"):
            await cli.chat(
                messages=[{"role": "user", "content": "x"}],
                thinking={"type": "low"},
            )


async def test_embed_passes_dimensions() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1] * 4}], "usage": {"prompt_tokens": 3}},
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        resp = await cli.embed(["query"], dimensions=1024)

    assert captured["body"]["dimensions"] == 1024
    # M7.5 hotfix：LiteLLM 透传 voyage 时只认 voyage 自家的 `output_dimension`，
    # OpenAI 标准的 `dimensions` 被忽略，导致 voyage 返回默认 2048。这里同时塞两个
    # 字段做双协议兼容（未识别字段任一上游 schema 都会忽略）。
    assert captured["body"]["output_dimension"] == 1024
    assert captured["body"]["model"] == "voyage-4-large"
    assert resp["data"][0]["embedding"] == [0.1] * 4


async def test_embed_observation_never_records_vector(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.123, 0.456]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        await cli.embed(["private embedding input"], dimensions=2)

    assert observation.start["as_type"] == "embedding"
    assert observation.start["input"] == {"input_count": 1, "input_chars": 23}
    success = observation.successes[0]
    assert success["output"] == {"embedding_count": 1, "dimensions": [2], "indices": [0]}
    assert "0.123" not in repr(observation.start) + repr(success)
    assert success["usage_details"] == {"input": 4, "total": 4}


async def test_embed_default_model_follows_provider() -> None:
    """P1-2：embed 缺省模型随 EMBEDDING_PROVIDER（openai → text-embedding-3-large），
    不再写死 voyage——否则 provider=openai 时 query 仍被 voyage 编码。"""
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1] * 4}], "usage": {"prompt_tokens": 3}},
        )

    s = _settings(EMBEDDING_PROVIDER="openai", OPENAI_EMBEDDING_MODEL="text-embedding-3-large")
    async with LiteLLMClient(settings=s, client=_mock_client(handler)) as cli:
        await cli.embed(["query"])

    assert captured["body"]["model"] == "text-embedding-3-large"


async def test_embed_uses_settings_default_dimension() -> None:
    """未显式传 dimensions → fallback 到 Settings.EMBEDDING_DIMENSIONS；双字段都写。"""
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1] * 4}], "usage": {"prompt_tokens": 3}},
        )

    async with LiteLLMClient(
        settings=_settings(EMBEDDING_DIMENSIONS=1024), client=_mock_client(handler)
    ) as cli:
        await cli.embed(["query"])

    assert captured["body"]["dimensions"] == 1024
    assert captured["body"]["output_dimension"] == 1024


async def test_rerank_parses_results_field() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                ]
            },
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        ranks = await cli.rerank(query="q", documents=["a", "b"], top_k=2)
    assert ranks == [
        {"index": 1, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.4},
    ]
    assert captured["body"]["top_n"] == 2
    assert "top_k" not in captured["body"]


async def test_rerank_observation_records_counts_not_documents(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [{"index": 1, "relevance_score": 0.9}],
                "usage": {"total_tokens": 20},
            },
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        result = await cli.rerank(
            query="private query",
            documents=["private document one", "private document two"],
            top_k=1,
        )

    assert result == [{"index": 1, "relevance_score": 0.9}]
    assert observation.start["as_type"] == "span"
    assert observation.start["metadata"]["operation"] == "rerank"
    assert observation.start["input"]["document_count"] == 2
    assert "private query" not in repr(observation.start)
    assert "private document" not in repr(observation.start)
    success = observation.successes[0]
    assert success["output"]["results"] == [{"index": 1, "relevance_score": 0.9}]
    assert success["metadata"]["usage_source"] == "provider"


async def test_rerank_falls_back_to_data_field() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "score": 0.7}]})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        ranks = await cli.rerank(query="q", documents=["a"])
    assert ranks == [{"index": 0, "relevance_score": 0.7}]


async def test_rerank_malformed_response_finishes_error_observation(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"relevance_score": 0.7}]})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        with pytest.raises(KeyError, match="index"):
            await cli.rerank(query="q", documents=["a"])

    error, telemetry = observation.errors[0]
    assert isinstance(error, KeyError)
    assert telemetry.status_code == 200


async def test_chat_stream_parses_sse() -> None:
    async def stream_body() -> bytes:
        return b""

    captured_body: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(req.content))
        body = (
            b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        chunks = [c async for c in cli.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert [c["choices"][0]["delta"]["content"] for c in chunks] == ["He", "llo"]
    assert "stream_options" not in captured_body


async def test_chat_stream_observation_records_ttft_usage_and_output(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)
    captured_body: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(req.content))
        body = (
            b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"llo"},"finish_reason":"stop"}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":5,'
            b'"completion_tokens":2,"total_tokens":7}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        chunks = [c async for c in cli.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert len(chunks) == 3
    assert captured_body["stream_options"] == {"include_usage": True}
    success = observation.successes[0]
    assert success["output"] == {
        "choice_count": 1,
        "content_chars": 5,
        "finish_reasons": ["stop"],
    }
    assert isinstance(success["completion_start_time"], datetime)
    assert success["usage_details"] == {"input": 5, "output": 2, "total": 7}


async def test_chat_stream_empty_success_does_not_invent_usage(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        assert [chunk async for chunk in cli.chat_stream(messages=[])] == []

    success = observation.successes[0]
    assert success["completion_start_time"] is None
    assert success["usage_details"] is None
    assert success["cost_details"] is None


async def test_chat_stream_http_error_finishes_observation(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"private gateway failure")

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        with pytest.raises(LLMError, match="HTTP 502"):
            async for _ in cli.chat_stream(messages=[]):
                pass

    error, telemetry = observation.errors[0]
    assert isinstance(error, LLMError)
    assert telemetry.status_code == 502
    assert "private gateway failure" not in repr(observation.errors)


async def test_chat_stream_timeout_is_wrapped_and_finishes_observation(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        with pytest.raises(LLMError, match="network error"):
            async for _ in cli.chat_stream(messages=[]):
                pass

    error, telemetry = observation.errors[0]
    assert isinstance(error, httpx.ReadTimeout)
    assert telemetry.status_code is None


async def test_chat_stream_explicit_close_finishes_cancelled_observation(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"second"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream"},
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        stream = cli.chat_stream(messages=[{"role": "user", "content": "close me"}])
        assert (await anext(stream))["choices"][0]["delta"]["content"] == "first"
        await stream.aclose()

    error, telemetry = observation.errors[0]
    assert isinstance(error, GeneratorExit)
    assert telemetry.status_code == 200


async def test_chat_stream_cancellation_finishes_observation(monkeypatch: Any) -> None:
    observation = _capture_observation(monkeypatch)

    class _BlockingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.waiting = asyncio.Event()

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            self.waiting.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            return None

    body = _BlockingStream()

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=body,
            headers={"content-type": "text/event-stream"},
        )

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        stream = cli.chat_stream(messages=[{"role": "user", "content": "cancel me"}])
        first = await anext(stream)
        assert first["choices"][0]["delta"]["content"] == "first"
        next_chunk = asyncio.create_task(anext(stream))
        await asyncio.wait_for(body.waiting.wait(), timeout=1)
        next_chunk.cancel()
        with pytest.raises(asyncio.CancelledError):
            await next_chunk

    error, telemetry = observation.errors[0]
    assert isinstance(error, asyncio.CancelledError)
    assert telemetry.status_code == 200
