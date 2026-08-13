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

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
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
from app.llm.langfuse_observability import (
    LangfuseObservation,
    RequestTelemetry,
    chat_cost_details,
    chat_input,
    chat_output,
    chat_usage_details,
    embedding_cost_details,
    embedding_input,
    embedding_output,
    embedding_usage_details,
    model_parameters,
    rerank_input,
    rerank_output,
)
from app.llm.pricing import rerank_billable_tokens, rerank_cost_usd

log = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 0


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


def _stream_content_parts(chunk: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    choices = chunk.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
    return parts


def _stream_finish_reasons(chunk: dict[str, Any]) -> list[str]:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return []
    return [
        str(choice["finish_reason"])
        for choice in choices
        if isinstance(choice, dict) and choice.get("finish_reason") is not None
    ]


def _rerank_accounting(
    *, query: str, documents: Sequence[str], response: dict[str, Any]
) -> dict[str, Any]:
    query_tokens = _approx_token_count(query)
    document_tokens = sum(_approx_token_count(document) for document in documents)
    usage_value = response.get("usage")
    usage = usage_value if isinstance(usage_value, dict) else {}
    meta_value = response.get("meta")
    meta = meta_value if isinstance(meta_value, dict) else {}
    tokens_value = meta.get("tokens")
    tokens = tokens_value if isinstance(tokens_value, dict) else {}
    try:
        meta_total = max(int(usage.get("total_tokens") or tokens.get("input_tokens") or 0), 0)
    except (TypeError, ValueError):
        meta_total = 0
    usage_source = "provider" if meta_total > 0 else "approximate"
    if meta_total > 0 and documents:
        document_tokens = max(meta_total - query_tokens * len(documents), document_tokens)
    return {
        "query_tokens": query_tokens,
        "document_tokens": document_tokens,
        "billable_tokens": rerank_billable_tokens(
            query_tokens=query_tokens,
            doc_tokens=document_tokens,
            n_docs=len(documents),
        ),
        "usage_source": usage_source,
    }


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
            "X-Request-ID": uuid.uuid4().hex,
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
        thinking: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """非流式 chat completion；返回 OpenAI 兼容 payload。

        `thinking`：mimo-v2.5/-pro 专属 reasoning 控制（见 xiaomimimo OpenAI 兼容
        API 文档 "thinking" 参数）。默认 `enabled`；短输出结构化节点（classify /
        rewrite / multi_query / self_rag / session_title）传 `{"type":"disabled"}`
        来：(1) 让 `temperature=0` 真生效（思考模式下 mimo 强制 temp=1.0，无法
        确定性输出）；(2) 把 reasoning_tokens 削为 0，节省成本与延迟。
        客户端发送统一 `reasoning_control`，共享网关按实际 Provider 映射到
        `extra_body.thinking`。
        """
        body = self._build_chat_body(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=False,
            thinking=thinking,
            extra=extra,
        )
        telemetry = RequestTelemetry()
        observation = LangfuseObservation.start(
            settings=self._settings,
            name="litellm.chat",
            as_type="generation",
            input=chat_input(self._settings, messages),
            model=str(body["model"]),
            model_parameters=model_parameters(body),
            metadata={"operation": "chat", "streaming": False},
        )
        try:
            resp = await self._post_json("/chat/completions", body, telemetry=telemetry)
            usage_details = chat_usage_details(resp)
            observation.finish_success(
                output=chat_output(self._settings, response=resp),
                telemetry=telemetry,
                usage_details=usage_details,
                cost_details=chat_cost_details(
                    model=str(body["model"]), response=resp, usage=usage_details
                ),
                metadata={"usage_source": "provider" if usage_details is not None else "missing"},
            )
            self._record_chat_usage(model_name=body["model"], resp=resp)
            return resp
        except BaseException as exc:
            observation.finish_error(exc, telemetry=telemetry)
            raise

    async def chat_stream(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: dict[str, Any] | None = None,
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
            thinking=thinking,
            extra=extra,
        )
        url = f"{self.base_url}/chat/completions"
        telemetry = RequestTelemetry(attempts=1)
        observation = LangfuseObservation.start(
            settings=self._settings,
            name="litellm.chat_stream",
            as_type="generation",
            input=chat_input(self._settings, messages),
            model=str(body["model"]),
            model_parameters=model_parameters(body),
            metadata={"operation": "chat", "streaming": True},
        )
        if observation.active:
            # Traced calls request a final usage-only chunk for generation cost.
            # Untraced calls retain the pre-PR2 request and yield contract exactly.
            body.setdefault("stream_options", {"include_usage": True})
        completion_start_time: datetime | None = None
        content_parts: list[str] = []
        finish_reasons: list[str] = []
        usage_response: dict[str, Any] = {}
        try:
            async with self._client.stream("POST", url, headers=self._headers, json=body) as resp:
                telemetry.status_code = resp.status_code
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
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    chunk_content_parts = _stream_content_parts(chunk)
                    # Role-only and final usage-only chunks are not first-token events.
                    if completion_start_time is None and chunk_content_parts:
                        completion_start_time = datetime.now(UTC)
                    content_parts.extend(chunk_content_parts)
                    finish_reasons.extend(_stream_finish_reasons(chunk))
                    if isinstance(chunk.get("usage"), dict):
                        usage_response = {"usage": chunk["usage"]}
                    yield chunk
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            wrapped = LLMError(f"chat_stream network error: {exc}")
            observation.finish_error(exc, telemetry=telemetry)
            raise wrapped from exc
        except BaseException as exc:
            observation.finish_error(exc, telemetry=telemetry)
            raise
        usage_details = chat_usage_details(usage_response)
        observation.finish_success(
            output=chat_output(
                self._settings,
                streamed_content="".join(content_parts),
                finish_reasons=finish_reasons,
            ),
            telemetry=telemetry,
            usage_details=usage_details,
            cost_details=chat_cost_details(
                model=str(body["model"]), response=usage_response, usage=usage_details
            ),
            completion_start_time=completion_start_time,
            metadata={"usage_source": "provider" if usage_details is not None else "missing"},
        )
        if usage_details is not None:
            self._record_chat_usage(model_name=body["model"], resp=usage_response)

    def _build_chat_body(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
        stream: bool,
        thinking: dict[str, Any] | None,
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
        if thinking is not None:
            thinking_type = thinking.get("type")
            if thinking_type not in {"enabled", "disabled"}:
                raise ValueError("thinking.type must be enabled or disabled")
            body["reasoning_control"] = thinking_type
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

        target dimension 同时塞 OpenAI 风格 `dimensions` 与 Voyage 风格 `output_dimension`：
        LiteLLM 透传 voyage 时只认 `output_dimension`，OpenAI / Azure 上游只认 `dimensions`；
        两个字段并存对任一上游 schema 都是合法 superset（未识别字段会被忽略），
        但缺 `output_dimension` 时 voyage 一直返回模型默认 2048 → 与 Qdrant
        `tgpp_chunks_voyage_d1024` collection 维度不匹配 → 400 Bad Request → backend
        生产 dense 路径一直 fallback 到 sparse-only（2026-05-22 M7.5 启动盘点时发现）。
        """
        body: dict[str, Any] = {
            # provider-aware：缺省按 EMBEDDING_PROVIDER 选模型（voyage/glm/openai），
            # 不再写死 voyage——否则 EMBEDDING_PROVIDER=openai 时 query 仍用 voyage 编码。
            "model": model or self._settings.embedding_model,
            "input": list(inputs),
        }
        target_dim = dimensions if dimensions is not None else self._settings.EMBEDDING_DIMENSIONS
        if target_dim is not None:
            body["dimensions"] = int(target_dim)
            body["output_dimension"] = int(target_dim)
        telemetry = RequestTelemetry()
        observation = LangfuseObservation.start(
            settings=self._settings,
            name="litellm.embedding",
            as_type="embedding",
            input=embedding_input(self._settings, inputs),
            model=str(body["model"]),
            model_parameters=model_parameters(body),
            metadata={"operation": "embedding"},
        )
        try:
            resp = await self._post_json("/embeddings", body, telemetry=telemetry)
            usage_details = embedding_usage_details(resp)
            observation.finish_success(
                output=embedding_output(resp),
                telemetry=telemetry,
                usage_details=usage_details,
                cost_details=embedding_cost_details(
                    model=str(body["model"]), response=resp, usage=usage_details
                ),
                metadata={
                    "usage_source": "provider" if usage_details is not None else "missing",
                    "vectors_recorded": False,
                },
            )
            self._record_embedding_usage(model_name=body["model"], inputs=list(inputs), resp=resp)
            return resp
        except BaseException as exc:
            observation.finish_error(exc, telemetry=telemetry)
            raise

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
            body["top_n"] = int(top_k)
        telemetry = RequestTelemetry()
        observation = LangfuseObservation.start(
            settings=self._settings,
            name="litellm.rerank",
            as_type="span",
            input=rerank_input(
                self._settings,
                query=query,
                documents=documents,
                top_n=top_k,
            ),
            metadata={
                "operation": "rerank",
                "model": body["model"],
                **model_parameters(body),
            },
        )
        try:
            payload = await self._post_json("/rerank", body, telemetry=telemetry)
            self._record_rerank_usage(
                model_name=body["model"], query=query, documents=list(documents), resp=payload
            )
            results = payload.get("results") or payload.get("data") or []
            parsed = [
                {
                    "index": int(item["index"]),
                    "relevance_score": float(
                        item.get("relevance_score") or item.get("score") or 0.0
                    ),
                }
                for item in results
            ]
            accounting = _rerank_accounting(query=query, documents=documents, response=payload)
            observation.finish_success(
                output=rerank_output(parsed),
                telemetry=telemetry,
                metadata={
                    "usage_source": accounting["usage_source"],
                    "query_tokens": accounting["query_tokens"],
                    "document_tokens": accounting["document_tokens"],
                    "billable_tokens": accounting["billable_tokens"],
                    "estimated_cost_usd": rerank_cost_usd(
                        str(body["model"]),
                        query_tokens=accounting["query_tokens"],
                        doc_tokens=accounting["document_tokens"],
                        n_docs=len(documents),
                    ),
                    "full_documents_recorded": False,
                },
            )
            return parsed
        except BaseException as exc:
            observation.finish_error(exc, telemetry=telemetry)
            raise

    # ---------- core ----------

    async def _post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        telemetry: RequestTelemetry | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential(min=0.5, max=8),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                try:
                    if telemetry is not None:
                        telemetry.attempts += 1
                    resp = await self._client.post(url, headers=self._headers, json=body)
                    if telemetry is not None:
                        telemetry.status_code = resp.status_code
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
