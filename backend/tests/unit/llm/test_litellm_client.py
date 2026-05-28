"""LiteLLMClient 行为测试（mock transport）。

不连真实 LiteLLM；通过 httpx.MockTransport 注入响应。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import LLMError
from app.llm.litellm_client import LiteLLMClient


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


async def test_chat_thinking_wraps_into_extra_body() -> None:
    """mimo `thinking` 是 provider-specific 参数，LiteLLM proxy 直接 top-level 透
    传会被 OpenAI spec 校验剥掉（实测 reasoning_tokens 不变）；必须用 `extra_body`
    包裹。本测锁住这一规则不被悄悄退回。"""
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
    assert "thinking" not in body  # 不能 top-level
    assert body["extra_body"]["thinking"] == {"type": "disabled"}


async def test_chat_4xx_raises_llm_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        with pytest.raises(LLMError) as ei:
            await cli.chat(messages=[{"role": "user", "content": "x"}])
    assert "HTTP 400" in ei.value.message


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
    def handler(_req: httpx.Request) -> httpx.Response:
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


async def test_rerank_falls_back_to_data_field() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "score": 0.7}]})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        ranks = await cli.rerank(query="q", documents=["a"])
    assert ranks == [{"index": 0, "relevance_score": 0.7}]


async def test_chat_stream_parses_sse() -> None:
    async def stream_body() -> bytes:
        return b""

    def handler(_req: httpx.Request) -> httpx.Response:
        body = (
            b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async with LiteLLMClient(settings=_settings(), client=_mock_client(handler)) as cli:
        chunks = [c async for c in cli.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert [c["choices"][0]["delta"]["content"] for c in chunks] == ["He", "llo"]
