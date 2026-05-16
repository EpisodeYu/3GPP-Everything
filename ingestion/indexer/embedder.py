"""Embedding 客户端（统一走 LiteLLM proxy 的 OpenAI 兼容 /embeddings 端点）。

设计要点（docs §4.4 / §4.7）：

- **不直连 voyageai SDK** —— 所有调用走本机 LiteLLM proxy，便于统一限流、计费、降级。
- batch 64：单次 POST `/embeddings` 携带 ≤ 64 个文本；超过自动切片。
- 失败重试：tenacity 指数退避；区分 HTTP 4xx（不重试）vs 5xx/网络异常（重试 3 次）。
- 向量维度动态探测：首次成功调用后缓存 `dim` 给 QdrantWriter 建 collection；避免硬编码
  voyage-3-large(1024) / voyage-4-large(默认 1024，本项目 LiteLLM 显式配置 2048) /
  embedding-3(2048) 的差异。
- provider 抽象：`voyage` / `glm` 都通过 LiteLLM；二者仅 model name 不同。

接口契约：

```python
embedder = Embedder.from_env(provider="voyage")
result = embedder.embed_texts(["text1", "text2", ...])  # 自动 batch + retry
# → EmbeddingBatchResult(vectors=[...], dim=2048, model="voyage-4-large", prompt_tokens=N)
```

测试注入点：

- `http_client` 替身：覆盖 `_LiteLLMEmbeddingClient.embed`，按队列吐 payload
- `model` / `base_url` / `api_key` / `batch_size`：构造参数直接覆盖
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence
from typing import Any

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import EmbeddingBatchResult, MultiDimEmbeddingResult, Provider

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 64
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MULTIDIM_DIMS: tuple[int, ...] = (2048, 1024)


# 子项目用得到的 provider → env key 映射（与 .env.example §EMBEDDING / docs §4.4 对齐）
PROVIDER_MODEL_ENV = {
    "voyage": "VOYAGE_EMBEDDING_MODEL",
    "glm": "GLM_EMBEDDING_MODEL",
}
PROVIDER_DEFAULT_MODEL = {
    "voyage": "voyage-4-large",
    "glm": "embedding-3",
}


class EmbeddingError(Exception):
    """embedder 对外异常基类。"""


class _LiteLLMEmbeddingClient:
    """薄封装 httpx.Client，调 LiteLLM proxy 的 /embeddings。

    与 images/vision.py 中 _LiteLLMClient 平行；两者刻意不共享，因为：
    - vision 用 /chat/completions（多模态），timeout/重试策略不同
    - embedding 多次 batch，timeout 短、batch 失败需要细粒度分片重试
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> _LiteLLMEmbeddingClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def embed(self, *, model: str, inputs: Sequence[str], dimensions: int | None = None) -> dict:
        """单次 POST。返回原始 LiteLLM/OpenAI 兼容 payload。

        `dimensions` 透传给 voyage（MRL 模型按此截维返回）；None = 服务端默认。
        """
        body: dict = {"model": model, "input": list(inputs)}
        if dimensions is not None:
            body["dimensions"] = dimensions
        resp = self._client.post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


def _is_retryable_http(exc: BaseException) -> bool:
    """LiteLLM 5xx / 网络异常 / 超时重试；4xx 不重试（多半 schema 错）。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.RequestError | httpx.TimeoutException)


class Embedder:
    """Embedding 主入口，按 provider + model 调 LiteLLM proxy。

    构造参数：
      - http_client: 注入 `_LiteLLMEmbeddingClient`；为 None 时按 .env 自建
      - provider: "voyage" / "glm"
      - model: 显式覆盖；否则按 provider 读对应 env 或使用默认
      - batch_size: 单次 POST 携带的文本上限（默认 64）
      - max_retries: 5xx / 网络异常重试次数（不含首次）

    线程安全：本类内部不持久状态，httpx.Client 由 httpx 自身保证线程安全。
    """

    def __init__(
        self,
        *,
        http_client: _LiteLLMEmbeddingClient | None = None,
        provider: Provider = "voyage",
        model: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        dimensions: int | None = None,
    ) -> None:
        self._http = http_client or self._build_default_http_client()
        self._owns_http = http_client is None
        self.provider: Provider = provider
        self.model = model or _resolve_model(provider)
        self.batch_size = batch_size
        self.max_retries = max_retries
        # `dimensions` 透传给 LiteLLM /embeddings（voyage MRL 模型支持按维度截断）。
        # None = 用上游默认（LiteLLM proxy 的 config.yaml 已声明 voyage-4-large=2048）
        self.dimensions = dimensions
        self._dim: int | None = None

    @staticmethod
    def _build_default_http_client() -> _LiteLLMEmbeddingClient:
        base_url = os.environ.get("LITELLM_BASE_URL")
        api_key = os.environ.get("LITELLM_API_KEY")
        if not base_url or not api_key:
            raise EmbeddingError(
                "LITELLM_BASE_URL / LITELLM_API_KEY missing; "
                "either configure .env or pass http_client= explicitly"
            )
        return _LiteLLMEmbeddingClient(base_url=base_url, api_key=api_key)

    @classmethod
    def from_env(cls, provider: Provider = "voyage", **kwargs: Any) -> Embedder:
        """读 .env 一次性构造 Embedder（CLI / pipeline 用）。"""
        return cls(provider=provider, **kwargs)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Embedder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def dim(self) -> int | None:
        """已探测到的向量维度；首次 `embed_texts` 后非 None。"""
        return self._dim

    def warmup(self) -> int:
        """主动探测向量维度（调一次 dummy embedding）。

        QdrantWriter 建 collection 前调一次即可，避免 pipeline 主流程被空 chunks 卡住。
        """
        result = self.embed_texts(["warmup"])
        return result.dim

    def embed_texts(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        """对一批文本生成 embedding。

        - 文本数量 > batch_size 时自动切片，按顺序拼回单个 EmbeddingBatchResult
        - 任一 batch 内的 5xx / 网络异常 retry max_retries 次
        - 任一 batch 失败到放弃 → 整体抛 EmbeddingError，调用方自行决定 spec 级回滚
        """
        if not texts:
            return EmbeddingBatchResult(vectors=[], dim=self._dim or 0, model=self.model)

        vectors: list[list[float]] = []
        prompt_tokens = 0
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            try:
                payload = self._embed_with_retry(batch)
            except RetryError as exc:
                last = exc.last_attempt.exception()
                raise EmbeddingError(
                    f"embedding batch failed after retries (start={start}, "
                    f"size={len(batch)}): {type(last).__name__}: {last}"
                ) from last
            data = payload.get("data") or []
            if len(data) != len(batch):
                raise EmbeddingError(
                    f"embedding response size mismatch: expected {len(batch)}, got {len(data)}"
                )
            for item in sorted(data, key=lambda x: x.get("index", 0)):
                vec = item.get("embedding") or []
                if not vec:
                    raise EmbeddingError(f"empty embedding for index={item.get('index')}")
                vectors.append(list(vec))
            usage = payload.get("usage") or {}
            prompt_tokens += int(usage.get("prompt_tokens") or 0)

        dim = len(vectors[0])
        if any(len(v) != dim for v in vectors):
            raise EmbeddingError("inconsistent embedding dim within single batch result")
        if self._dim is None:
            self._dim = dim
            log.info("embedder dim detected: %d (model=%s)", dim, self.model)
        elif self._dim != dim:
            raise EmbeddingError(
                f"embedding dim drift: cached={self._dim} got={dim} (model changed?)"
            )

        return EmbeddingBatchResult(
            vectors=vectors,
            dim=dim,
            model=self.model,
            prompt_tokens=prompt_tokens,
        )

    def embed_texts_multidim(
        self,
        texts: Sequence[str],
        *,
        dims: Sequence[int] = DEFAULT_MULTIDIM_DIMS,
    ) -> MultiDimEmbeddingResult:
        """一次 API 调用 + 客户端 truncate+L2 renorm 派生其他维度（M2 §4.7 / B0 等价性）。

        - 调用 API 时强制 `dimensions=max(dims)`（暂存 self.dimensions，调用后恢复）
        - 其他档：取前 N 维 + L2 renormalize
        - B0 spike 已验证 cosine(truncate, direct_call) median=1.0 / min=1.0

        约束：
          - dims 必须全部 ≤ self.model 支持的最大维度（voyage-4-large=2048）
          - dims 须严格递减或递增不重要，方法内部按降序处理
        """
        if not texts:
            return MultiDimEmbeddingResult(
                vectors_by_dim={d: [] for d in dims}, dim_main=max(dims), model=self.model
            )
        if not dims:
            raise EmbeddingError("dims must be non-empty")
        unique_sorted = sorted({int(d) for d in dims}, reverse=True)
        if any(d <= 0 for d in unique_sorted):
            raise EmbeddingError(f"dims must be positive, got {dims}")
        dim_main = unique_sorted[0]

        prev_dimensions = self.dimensions
        self.dimensions = dim_main
        try:
            base = self.embed_texts(texts)
        finally:
            self.dimensions = prev_dimensions

        if base.dim != dim_main:
            raise EmbeddingError(f"multidim main call returned dim={base.dim}, expected {dim_main}")
        out: dict[int, list[list[float]]] = {dim_main: base.vectors}
        for sub in unique_sorted[1:]:
            if sub > dim_main:
                raise EmbeddingError(f"sub dim {sub} > main dim {dim_main}")
            out[sub] = [_truncate_and_renorm(v, sub) for v in base.vectors]
        return MultiDimEmbeddingResult(
            vectors_by_dim=out,
            dim_main=dim_main,
            model=base.model,
            prompt_tokens=base.prompt_tokens,
        )

    def _embed_with_retry(self, batch: list[str]) -> dict:
        retrying = retry(
            reraise=False,
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(
                (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException)
            ),
            before_sleep=_log_retry,
        )

        @retrying
        def _call() -> dict:
            try:
                return self._http.embed(model=self.model, inputs=batch, dimensions=self.dimensions)
            except httpx.HTTPStatusError as exc:
                if not _is_retryable_http(exc):
                    raise EmbeddingError(
                        f"embedding HTTP {exc.response.status_code}: {exc.response.text[:300]}"
                    ) from exc
                raise

        return _call()


def _resolve_model(provider: Provider) -> str:
    env_key = PROVIDER_MODEL_ENV.get(provider)
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    return PROVIDER_DEFAULT_MODEL.get(provider, "voyage-4-large")


def _log_retry(retry_state: Any) -> None:
    log.warning(
        "embedding retry (attempt %d): %s",
        retry_state.attempt_number,
        retry_state.outcome.exception() if retry_state.outcome else None,
    )


# -------------------- 辅助：truncate + L2 renormalize（MRL） --------------------


def _truncate_and_renorm(vec: Sequence[float], dim: int) -> list[float]:
    """取前 dim 维 + L2 renormalize（matryoshka 派生）。"""
    if dim > len(vec):
        raise EmbeddingError(
            f"truncate target dim {dim} > source len {len(vec)}; can't matryoshka-up"
        )
    head = list(vec[:dim])
    norm = sum(x * x for x in head) ** 0.5
    if norm == 0.0:
        return head
    return [x / norm for x in head]


# -------------------- 辅助：批量 embed Chunk 列表 --------------------


def embed_chunks(
    embedder: Embedder, chunks: Sequence[Any], *, content_attr: str = "content"
) -> EmbeddingBatchResult:
    """对一批 Chunk dataclass 抽 content 字段，过一次 embedder。

    保持 chunks 顺序 = vectors 顺序，调用方按 idx 配对。
    """
    texts = [getattr(c, content_attr) for c in chunks]
    t0 = time.time()
    result = embedder.embed_texts(texts)
    log.info(
        "embedded %d chunks in %.1fs (model=%s, dim=%d, tokens=%d)",
        len(texts),
        time.time() - t0,
        result.model,
        result.dim,
        result.prompt_tokens,
    )
    return result


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MULTIDIM_DIMS",
    "DEFAULT_TIMEOUT_S",
    "PROVIDER_DEFAULT_MODEL",
    "PROVIDER_MODEL_ENV",
    "Embedder",
    "EmbeddingError",
    "embed_chunks",
]
