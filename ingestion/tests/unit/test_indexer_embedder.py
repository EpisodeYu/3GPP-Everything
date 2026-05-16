"""Embedder 单测。

覆盖：
- 单 batch 成功 → vectors / dim / model / prompt_tokens 正确
- 多 batch 切分（> batch_size）
- 维度不一致跨 batch 抛异常
- 5xx → tenacity 重试 → 最终成功
- 4xx → 立即失败（不重试）
- 空文本列表 → 空结果
- response 顺序错乱（index 字段）→ 按 index 排序回正
"""

from __future__ import annotations

import httpx
import pytest

from ingestion.indexer.embedder import (
    Embedder,
    EmbeddingError,
    _LiteLLMEmbeddingClient,
    embed_chunks,
)


class _StubHttp(_LiteLLMEmbeddingClient):
    """LiteLLMEmbeddingClient 替身：按队列吐 payload，或抛异常。"""

    def __init__(self, *, responses: list[object]) -> None:
        self.base_url = "http://stub"
        self.api_key = "stub"
        self._owns_client = False
        self._client = None  # type: ignore[assignment]
        self._responses = list(responses)
        self.calls: list[tuple[str, list[str]]] = []

    def embed(self, *, model: str, inputs, dimensions: int | None = None):  # type: ignore[override]
        self.calls.append((model, list(inputs)))
        # 测试可在调用后 inspect self.last_dimensions 校验 multidim 路径透传
        self.last_dimensions = dimensions
        if not self._responses:
            raise AssertionError("StubHttp out of responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _payload(vectors: list[list[float]], *, prompt_tokens: int = 0, model: str = "voyage-4-large"):
    return {
        "model": model,
        "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
        "usage": {"prompt_tokens": prompt_tokens},
    }


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """构造一个 HTTPStatusError 实例（不需要真发请求）。"""
    request = httpx.Request("POST", "http://stub/embeddings")
    response = httpx.Response(status_code, request=request, text=f"err{status_code}")
    return httpx.HTTPStatusError(f"{status_code}", request=request, response=response)


def test_embed_single_batch_success() -> None:
    http = _StubHttp(responses=[_payload([[0.1, 0.2, 0.3]], prompt_tokens=10)])
    with Embedder(http_client=http, provider="voyage", model="voyage-4-large") as emb:
        result = emb.embed_texts(["hello"])
    assert result.vectors == [[0.1, 0.2, 0.3]]
    assert result.dim == 3
    assert result.model == "voyage-4-large"
    assert result.prompt_tokens == 10
    assert len(http.calls) == 1


def test_embed_multi_batch_splits_correctly() -> None:
    # 130 texts, batch_size=64 → 3 batches (64+64+2)
    http = _StubHttp(
        responses=[
            _payload([[0.1, 0.2]] * 64),
            _payload([[0.3, 0.4]] * 64),
            _payload([[0.5, 0.6]] * 2, prompt_tokens=2),
        ]
    )
    with Embedder(http_client=http, batch_size=64) as emb:
        result = emb.embed_texts(["t"] * 130)
    assert len(result.vectors) == 130
    assert all(len(v) == 2 for v in result.vectors)
    assert len(http.calls) == 3
    assert [len(call[1]) for call in http.calls] == [64, 64, 2]


def test_embed_dim_drift_raises() -> None:
    # 第一批 dim=2，第二批 dim=3 → 跨 batch 不一致
    http = _StubHttp(
        responses=[
            _payload([[0.1, 0.2]] * 2),
            _payload([[0.3, 0.4, 0.5]] * 2),
        ]
    )
    with Embedder(http_client=http, batch_size=2) as emb, pytest.raises(EmbeddingError):
        emb.embed_texts(["a", "b", "c", "d"])


def test_embed_response_size_mismatch() -> None:
    http = _StubHttp(responses=[_payload([[0.1, 0.2]])])  # 只返回 1 个
    with Embedder(http_client=http) as emb, pytest.raises(EmbeddingError):
        emb.embed_texts(["a", "b"])  # 期望 2


def test_embed_empty_input_returns_empty() -> None:
    http = _StubHttp(responses=[])
    with Embedder(http_client=http) as emb:
        result = emb.embed_texts([])
    assert result.vectors == []
    assert result.dim == 0
    assert http.calls == []


def test_embed_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # 减小 tenacity 的 wait 避免测试慢
    monkeypatch.setattr(
        "ingestion.indexer.embedder.wait_exponential",
        lambda **_: lambda *_args, **_kwargs: 0,
    )
    http = _StubHttp(
        responses=[
            _http_status_error(503),
            _http_status_error(500),
            _payload([[0.1, 0.2]]),
        ]
    )
    with Embedder(http_client=http, max_retries=3) as emb:
        result = emb.embed_texts(["x"])
    assert result.vectors == [[0.1, 0.2]]
    assert len(http.calls) == 3


def test_embed_does_not_retry_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ingestion.indexer.embedder.wait_exponential",
        lambda **_: lambda *_args, **_kwargs: 0,
    )
    http = _StubHttp(responses=[_http_status_error(400)])
    with Embedder(http_client=http, max_retries=3) as emb, pytest.raises(EmbeddingError):
        emb.embed_texts(["x"])
    assert len(http.calls) == 1


def test_embed_handles_response_index_out_of_order() -> None:
    # LiteLLM/voyage 偶尔会乱序返回；data[i].index 才是真实位置
    payload = {
        "model": "m",
        "data": [
            {"index": 2, "embedding": [0.3]},
            {"index": 0, "embedding": [0.1]},
            {"index": 1, "embedding": [0.2]},
        ],
        "usage": {"prompt_tokens": 3},
    }
    http = _StubHttp(responses=[payload])
    with Embedder(http_client=http) as emb:
        result = emb.embed_texts(["a", "b", "c"])
    assert result.vectors == [[0.1], [0.2], [0.3]]


def test_embed_chunks_helper() -> None:
    class _C:
        def __init__(self, content: str) -> None:
            self.content = content

    http = _StubHttp(responses=[_payload([[1.0], [2.0]])])
    chunks = [_C("a"), _C("b")]
    with Embedder(http_client=http) as emb:
        result = embed_chunks(emb, chunks)
    assert result.vectors == [[1.0], [2.0]]
    assert http.calls[0][1] == ["a", "b"]


def test_embed_warmup_sets_dim() -> None:
    http = _StubHttp(responses=[_payload([[0.1, 0.2, 0.3, 0.4]])])
    with Embedder(http_client=http) as emb:
        assert emb.dim is None
        d = emb.warmup()
        assert d == 4
        assert emb.dim == 4
