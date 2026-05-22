"""LiteLLM proxy 客户端（async）。

backend / agent 内所有 LLM 调用统一走本机 LiteLLM proxy（OpenAI 兼容 endpoint），
不直连各上游 SDK。理由（与 ingestion/indexer/embedder.py 一致）：

- 限流 / 计费 / 降级在 LiteLLM 层集中管理
- 切上游模型只改 LiteLLM `config.yaml`，业务代码不动

只暴露三个 method：

- `chat()` / `chat_stream()` —— /chat/completions（agent.generate / classify 等用）
- `embed()` —— /embeddings（dense retriever 用，rerank 之前先把 query 也编一次时用）
- `rerank()` —— /rerank（LiteLLM 透传 voyage `rerank-2.5`）

为什么和 ingestion 端 embedder.py 不共享：
- 那个是 sync httpx.Client（pipeline 串行）；backend 是 async httpx.AsyncClient
  （FastAPI / agent 内全 async）
- 拆开避免 ingestion 把 backend 依赖（langchain / langgraph）拖进 ingestion 容器
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import Settings, get_settings
from app.core.errors import LLMError, UpstreamError

log = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3


def _approx_token_count(text: str) -> int:
    """粗估 token：4 字符 ≈ 1 token（OpenAI/Voyage 公开口径的下限近似）。

    rerank 路径上 LiteLLM 不一定回 usage 字段，需 fallback 估算。中文每字 ≈ 1 token
    比英文密集，4 字符近似下限对计费是保守低估，实务可接受（M7.4 Q2 仅 log warning，
    不依赖精确数字）。
    """
    return max(len(text) // 4, 1) if text else 0


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.RequestError | httpx.TimeoutException)


class LiteLLMClient:
    """Async httpx 客户端，包 LiteLLM proxy 的 /chat/completions /embeddings /rerank。

    用法：
        async with LiteLLMClient() as cli:
            resp = await cli.chat(messages=[...], model="mimo-v2.5")
            vec  = await cli.embed(["q"])
            ranks = await cli.rerank(query="q", documents=["a","b"], top_k=2)

    或注入到长生命周期 service（FastAPI lifespan），需 caller 显式 await close()。
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        timeout = httpx.Timeout(self._settings.LITELLM_TIMEOUT_S)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._max_retries = max_retries

    @property
    def base_url(self) -> str:
        return self._settings.LITELLM_BASE_URL.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.LITELLM_API_KEY.get_secret_value()}",
            "Content-Type": "application/json",
        }

    async def __aenter__(self) -> LiteLLMClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------- chat ----------

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """非流式 chat completion；返回 OpenAI 兼容 payload。"""
        body = self._build_chat_body(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=False,
            extra=extra,
        )
        resp = await self._post_json("/chat/completions", body)
        self._record_chat_usage(model_name=body["model"], resp=resp)
        return resp

    async def chat_stream(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """SSE 流式 chat；yield 每个 chunk dict（OpenAI 兼容）。

        注意：流式接口不在 tenacity retry 范围内（开始流之后再重试代价大）；
        网络抖动由 FastAPI 路由层捕获后转 SSE error event。
        """
        body = self._build_chat_body(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=None,
            stream=True,
            extra=extra,
        )
        url = f"{self.base_url}/chat/completions"
        try:
            async with self._client.stream("POST", url, headers=self._headers, json=body) as resp:
                if resp.status_code >= 400:
                    text = await resp.aread()
                    raise LLMError(
                        f"chat_stream HTTP {resp.status_code}",
                        details={"body": text.decode("utf-8", errors="replace")[:500]},
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        return
                    with contextlib.suppress(json.JSONDecodeError):
                        yield json.loads(payload)
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            raise LLMError(f"chat_stream network error: {exc}") from exc

    def _build_chat_body(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
        stream: bool,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or self._settings.LLM_AGENT_MODEL,
            "messages": list(messages),
            "stream": stream,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        if extra:
            body.update(extra)
        return body

    # ---------- embeddings ----------

    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> dict[str, Any]:
        """单次 /embeddings 调用；返回原 payload（含 data[].embedding 与 usage）。

        caller 自行切 batch（与 retrieval 的 query embedding 路径相对低频，单 batch 足够）。
        """
        body: dict[str, Any] = {
            "model": model or self._settings.VOYAGE_EMBEDDING_MODEL,
            "input": list(inputs),
        }
        target_dim = dimensions if dimensions is not None else self._settings.EMBEDDING_DIMENSIONS
        if target_dim is not None:
            body["dimensions"] = int(target_dim)
        resp = await self._post_json("/embeddings", body)
        self._record_embedding_usage(model_name=body["model"], inputs=list(inputs), resp=resp)
        return resp

    # ---------- rerank ----------

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        model: str | None = None,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """voyage rerank（LiteLLM 透传 /rerank）。

        返回 `[{"index": int, "relevance_score": float}]`，按分数降序，不含原 documents。
        """
        body: dict[str, Any] = {
            "model": model or self._settings.VOYAGE_RERANK_MODEL,
            "query": query,
            "documents": list(documents),
        }
        if top_k is not None:
            body["top_k"] = int(top_k)
        payload = await self._post_json("/rerank", body)
        self._record_rerank_usage(
            model_name=body["model"], query=query, documents=list(documents), resp=payload
        )
        results = payload.get("results") or payload.get("data") or []
        return [
            {
                "index": int(item["index"]),
                "relevance_score": float(item.get("relevance_score") or item.get("score") or 0.0),
            }
            for item in results
        ]

    # ---------- core ----------

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential(min=0.5, max=8),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                try:
                    resp = await self._client.post(url, headers=self._headers, json=body)
                    resp.raise_for_status()
                    return resp.json()
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status < 500:
                        # 4xx 不重试，直接转 LLMError 暴露上游响应
                        try:
                            err_body: Any = exc.response.json()
                        except ValueError:
                            err_body = exc.response.text[:500]
                        raise LLMError(
                            f"LiteLLM HTTP {status} on {path}",
                            details={"body": err_body},
                        ) from exc
                    raise
                except (httpx.RequestError, httpx.TimeoutException):
                    raise

        # 不可达
        raise UpstreamError("LiteLLM retry exhausted without exception")

    # ---------- usage hooks (M7.4) ----------
    # 设计：按 CLAUDE.md §3 surgical changes，hook 不改业务路径；任何异常 swallow。
    # 用户身份从 `app.services.usage.current_user_id` ContextVar 读，由 chat 路由
    # 在请求入口 set。无 user 上下文（ingestion / eval / 后台 job）→ skip。

    def _record_chat_usage(self, *, model_name: str, resp: dict[str, Any]) -> None:
        try:
            from app.services.usage import record_llm_usage, schedule_usage_hook

            usage = resp.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            if prompt_tokens <= 0 and completion_tokens <= 0:
                return
            schedule_usage_hook(
                record_llm_usage(
                    model=model_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            )
        except Exception as exc:
            log.debug("usage_hook chat failed: %s", exc)

    def _record_embedding_usage(
        self, *, model_name: str, inputs: list[str], resp: dict[str, Any]
    ) -> None:
        try:
            from app.services.usage import record_embedding_usage, schedule_usage_hook

            usage = resp.get("usage") or {}
            tokens = int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
            if tokens <= 0:
                tokens = sum(_approx_token_count(s) for s in inputs)
            if tokens <= 0:
                return
            schedule_usage_hook(record_embedding_usage(model=model_name, tokens=tokens))
        except Exception as exc:
            log.debug("usage_hook embedding failed: %s", exc)

    def _record_rerank_usage(
        self,
        *,
        model_name: str,
        query: str,
        documents: list[str],
        resp: dict[str, Any],
    ) -> None:
        """Voyage 口径：billable = query_tokens × n_docs + Σ doc_tokens。"""
        try:
            from app.services.usage import record_rerank_usage, schedule_usage_hook

            n_docs = len(documents)
            doc_tokens = sum(_approx_token_count(d) for d in documents)
            query_tokens = _approx_token_count(query)
            # LiteLLM 透传 voyage rerank 时若回 usage（meta.billed_units 等）也接受
            # 一下，比客户端估算更准；缺失走估算路径。
            usage = resp.get("usage") or {}
            meta_total = int(
                usage.get("total_tokens")
                or (resp.get("meta") or {}).get("tokens", {}).get("input_tokens")
                or 0
            )
            if meta_total > 0 and n_docs > 0:
                # 反推 doc_tokens（保留客户端估算的 query_tokens / n_docs）
                doc_tokens = max(meta_total - query_tokens * n_docs, doc_tokens)
            schedule_usage_hook(
                record_rerank_usage(
                    model=model_name,
                    query_tokens=query_tokens,
                    doc_tokens=doc_tokens,
                    n_docs=n_docs,
                )
            )
        except Exception as exc:
            log.debug("usage_hook rerank failed: %s", exc)
