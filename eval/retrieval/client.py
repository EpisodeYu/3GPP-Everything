"""LiteLLM embedding + Qdrant 客户端薄封装。

设计：
- 不依赖 ingestion 包：retrieval 评测应能脱离 indexer 独立跑
- voyage MRL：一次 API 调 max(dims)，客户端 truncate+L2 renorm 派生子维度
  （与 ingestion/indexer/embedder.py 的 _truncate_and_renorm 保持算法一致；
  M2 §B0 spike 已验证 cosine median=1.0 / min=1.0）
- 无 batch（评测每次 query 1 条），无 retry（单条失败抛即可）
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from qdrant_client import QdrantClient
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from eval.settings import EvalSettings, get_settings

log = logging.getLogger(__name__)


class EmbedError(Exception):
    """Embedding 客户端对外异常基类。"""


@dataclass(slots=True)
class EmbedResult:
    """单 query 多维度 embedding 结果。

    `vectors_by_dim[dim]` = 该 dim 下的 unit-norm 向量。
    """

    vectors_by_dim: dict[int, list[float]]
    dim_main: int
    model: str
    prompt_tokens: int = 0


def _truncate_and_renorm(vec: Sequence[float], dim: int) -> list[float]:
    """取前 dim 维 + L2 renormalize（matryoshka 派生）。

    与 ingestion/indexer/embedder.py 算法相同；故意复制以避免 eval 反向依赖 ingestion。
    """
    if dim > len(vec):
        raise EmbedError(f"truncate target dim {dim} > source len {len(vec)}; can't matryoshka-up")
    head = list(vec[:dim])
    norm = sum(x * x for x in head) ** 0.5
    if norm == 0.0:
        return head
    return [x / norm for x in head]


class LiteLLMEmbedClient:
    """单 query embedding 客户端（评测时 latency 比 batch 更重要，无切片）。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        http_client: httpx.Client | None = None,
        settings: EvalSettings | None = None,
    ) -> None:
        s = settings or get_settings()
        self.base_url = (base_url or s.resolved_litellm_base_url).rstrip("/")
        self.api_key = api_key or s.litellm_api_key
        self.model = model or s.voyage_embedding_model
        self.max_retries = max_retries
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(timeout_s))
        if not self.api_key:
            raise EmbedError("LITELLM_API_KEY missing; set in .env or pass api_key= explicitly")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LiteLLMEmbedClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def embed_query(self, text: str, *, dims: Sequence[int] = (2048, 1024)) -> EmbedResult:
        """对单个 query 文本 embed，按 dims 派生多维度向量。

        - 一次 API 调用：dimensions=max(dims)
        - 其他 dim：客户端 truncate(前 N 维) + L2 renormalize
        """
        if not text:
            raise EmbedError("empty query text")
        unique = sorted({int(d) for d in dims}, reverse=True)
        if not unique or unique[0] <= 0:
            raise EmbedError(f"invalid dims: {dims}")
        dim_main = unique[0]

        payload = self._call(text=text, dimensions=dim_main)
        data = payload.get("data") or []
        if len(data) != 1:
            raise EmbedError(f"embedding response size mismatch: expected 1, got {len(data)}")
        base_vec = data[0].get("embedding") or []
        if not base_vec:
            raise EmbedError("empty embedding vector")
        if len(base_vec) != dim_main:
            raise EmbedError(f"embedding dim mismatch: expected {dim_main}, got {len(base_vec)}")

        out: dict[int, list[float]] = {dim_main: list(base_vec)}
        for sub in unique[1:]:
            out[sub] = _truncate_and_renorm(base_vec, sub)

        usage = payload.get("usage") or {}
        return EmbedResult(
            vectors_by_dim=out,
            dim_main=dim_main,
            model=self.model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
        )

    def _call(self, *, text: str, dimensions: int) -> dict[str, Any]:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=15),
            retry=retry_if_exception_type(
                (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException)
            ),
            before_sleep=lambda rs: log.warning(
                "embed retry attempt=%d err=%s",
                rs.attempt_number,
                rs.outcome.exception() if rs.outcome else None,
            ),
        )
        def _do() -> dict[str, Any]:
            try:
                resp = self._client.post(
                    f"{self.base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self.model, "input": text, "dimensions": dimensions},
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise EmbedError(
                        f"embed HTTP {exc.response.status_code}: {exc.response.text[:300]}"
                    ) from exc
                raise

        try:
            return _do()
        except RetryError as exc:
            last = exc.last_attempt.exception()
            raise EmbedError(f"embed failed after retries: {type(last).__name__}: {last}") from last


def get_qdrant_client(settings: EvalSettings | None = None) -> QdrantClient:
    """单例 Qdrant client（评测全程共享一个连接池）。"""
    s = settings or get_settings()
    api_key = s.qdrant_api_key or None
    return QdrantClient(url=s.resolved_qdrant_url, api_key=api_key)


__all__ = [
    "EmbedError",
    "EmbedResult",
    "LiteLLMEmbedClient",
    "get_qdrant_client",
]
