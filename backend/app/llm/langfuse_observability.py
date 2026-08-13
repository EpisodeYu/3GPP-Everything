"""Fail-open Langfuse observations for the custom LiteLLM HTTP client.

Langfuse's LangChain callback creates the LangGraph/node spans.  The custom
``LiteLLMClient`` is not a LangChain model, so it creates its own child observations
only when a Langfuse/OpenTelemetry observation is already active.  Calls made by
background jobs or outside a traced graph never create orphan traces.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.core.config import Settings
from app.llm.pricing import (
    embedding_cost_usd,
    get_embedding_price,
    get_llm_price,
    llm_cost_usd,
)

log = logging.getLogger(__name__)

ObservationType = Literal["generation", "embedding", "span"]
_PREVIEW_ITEMS = 10
_PREVIEW_CHARS = 500


@dataclass(slots=True)
class RequestTelemetry:
    """HTTP facts collected without changing the returned business payload."""

    attempts: int = 0
    status_code: int | None = None

    @property
    def retry_count(self) -> int:
        return max(self.attempts - 1, 0)


class LangfuseObservation:
    """Small exception-swallowing wrapper around a Langfuse v4 observation."""

    __slots__ = ("_finished", "_raw")

    def __init__(self, raw: Any | None = None) -> None:
        self._raw = raw
        self._finished = False

    @property
    def active(self) -> bool:
        return self._raw is not None

    @classmethod
    def start(
        cls,
        *,
        settings: Settings,
        name: str,
        as_type: ObservationType,
        input: Any,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LangfuseObservation:
        try:
            # Runtime import avoids making the LLM layer initialize the agent package
            # during module import.  The graph callback has already initialized this
            # process singleton on traced requests.
            from app.agent.langfuse_handler import (
                current_langgraph_trace_context,
                init_langfuse,
            )

            trace_context = current_langgraph_trace_context()
            # ``start_observation`` creates a root span without an explicit parent.
            # PR2 deliberately records only node children, never background/orphan traces.
            if trace_context is None:
                return cls()
            client = init_langfuse(settings)
            if client is None:
                return cls()
            raw = client.start_observation(
                trace_context=trace_context,
                name=name,
                as_type=as_type,
                input=input,
                model=model,
                model_parameters=model_parameters,
                metadata=metadata,
            )
            return cls(raw)
        except Exception as exc:
            log.debug("langfuse observation start failed: %s", exc)
            return cls()

    def finish_success(
        self,
        *,
        output: Any,
        telemetry: RequestTelemetry,
        usage_details: dict[str, int] | None = None,
        cost_details: dict[str, float] | None = None,
        completion_start_time: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._finished:
            return
        merged_metadata = {
            "http_status": telemetry.status_code,
            "retry_count": telemetry.retry_count,
            **(metadata or {}),
        }
        self._update_and_end(
            output=output,
            metadata=merged_metadata,
            usage_details=usage_details,
            cost_details=cost_details,
            completion_start_time=completion_start_time,
        )

    def finish_error(self, exc: BaseException, *, telemetry: RequestTelemetry) -> None:
        if self._finished:
            return
        cancelled = isinstance(exc, (asyncio.CancelledError, GeneratorExit))
        if cancelled:
            status_message = "cancelled"
        elif telemetry.status_code is not None:
            status_message = f"http_{telemetry.status_code}"
        else:
            status_message = exc.__class__.__name__
        self._update_and_end(
            level="ERROR",
            status_message=status_message,
            metadata={
                "error_type": exc.__class__.__name__,
                "http_status": telemetry.status_code,
                "retry_count": telemetry.retry_count,
                "cancelled": cancelled,
            },
        )

    def _update_and_end(self, **kwargs: Any) -> None:
        self._finished = True
        if self._raw is None:
            return
        try:
            self._raw.update(**kwargs)
        except Exception as exc:
            log.debug("langfuse observation update failed: %s", exc)
        try:
            self._raw.end()
        except Exception as exc:
            log.debug("langfuse observation end failed: %s", exc)


def chat_input(settings: Settings, messages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    roles: dict[str, int] = {}
    content_chars = 0
    for message in messages:
        role = str(message.get("role") or "unknown")[:40]
        roles[role] = roles.get(role, 0) + 1
        content_chars += _value_chars(message.get("content"))
    result: dict[str, Any] = {
        "message_count": len(messages),
        "roles": roles,
        "content_chars": content_chars,
    }
    if settings.LANGFUSE_CAPTURE_CONTENT:
        result["messages"] = list(messages)
    return result


def chat_output(
    settings: Settings,
    *,
    response: dict[str, Any] | None = None,
    streamed_content: str | None = None,
    finish_reasons: Sequence[str] = (),
) -> dict[str, Any]:
    messages: list[Any] = []
    reasons = list(finish_reasons)
    if response is not None:
        choices = response.get("choices") or []
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            messages.append(choice.get("message") or {})
            reason = choice.get("finish_reason")
            if reason is not None:
                reasons.append(str(reason))
    content_chars = (
        len(streamed_content)
        if streamed_content is not None
        else sum(
            _value_chars(message.get("content"))
            for message in messages
            if isinstance(message, dict)
        )
    )
    result: dict[str, Any] = {
        "choice_count": len(messages) if response is not None else (1 if streamed_content else 0),
        "content_chars": content_chars,
        "finish_reasons": list(dict.fromkeys(reasons)),
    }
    if settings.LANGFUSE_CAPTURE_CONTENT:
        result["content"] = streamed_content if streamed_content is not None else messages
    return result


def embedding_input(settings: Settings, inputs: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "input_count": len(inputs),
        "input_chars": sum(len(item) for item in inputs),
    }
    if settings.APP_ENV == "dev" and settings.LANGFUSE_CAPTURE_CONTENT:
        result["input_preview"] = [_preview(item) for item in inputs[:_PREVIEW_ITEMS]]
    return result


def embedding_output(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data") or []
    dimensions: set[int] = set()
    indices: list[int] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        vector = item.get("embedding")
        if isinstance(vector, list):
            dimensions.add(len(vector))
        if isinstance(item.get("index"), int):
            indices.append(item["index"])
    return {
        "embedding_count": len(data) if isinstance(data, list) else 0,
        "dimensions": sorted(dimensions),
        "indices": indices[:_PREVIEW_ITEMS],
    }


def rerank_input(
    settings: Settings,
    *,
    query: str,
    documents: Sequence[str],
    top_n: int | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "query_chars": len(query),
        "document_count": len(documents),
        "document_chars": [len(document) for document in documents[:50]],
        "total_document_chars": sum(len(document) for document in documents),
        "top_n": top_n,
    }
    if settings.APP_ENV == "dev" and settings.LANGFUSE_CAPTURE_CONTENT:
        result["query_preview"] = _preview(query)
        result["document_previews"] = [
            _preview(document) for document in documents[:_PREVIEW_ITEMS]
        ]
    return result


def rerank_output(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "result_count": len(results),
        "results": [
            {
                "index": item.get("index"),
                "relevance_score": item.get("relevance_score", item.get("score")),
            }
            for item in results[:50]
            if isinstance(item, dict)
        ],
    }


def chat_usage_details(response: dict[str, Any]) -> dict[str, int] | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    input_present = "prompt_tokens" in usage or "input_tokens" in usage
    output_present = "completion_tokens" in usage or "output_tokens" in usage
    total_present = "total_tokens" in usage
    if not (input_present or output_present or total_present):
        return None
    input_tokens = _as_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    output_tokens = _as_int(usage.get("completion_tokens", usage.get("output_tokens")))
    total_tokens = _as_int(usage.get("total_tokens"))
    if not total_present:
        total_tokens = input_tokens + output_tokens
    result = {"input": input_tokens, "output": output_tokens, "total": total_tokens}
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    if isinstance(details, dict) and "reasoning_tokens" in details:
        result["reasoning"] = _as_int(details.get("reasoning_tokens"))
    return result


def embedding_usage_details(response: dict[str, Any]) -> dict[str, int] | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    if not any(key in usage for key in ("prompt_tokens", "input_tokens", "total_tokens")):
        return None
    input_tokens = _as_int(
        usage.get("prompt_tokens", usage.get("input_tokens", usage.get("total_tokens")))
    )
    total_tokens = _as_int(usage.get("total_tokens", input_tokens))
    return {"input": input_tokens, "total": total_tokens}


def chat_cost_details(
    *, model: str, response: dict[str, Any], usage: dict[str, int] | None
) -> dict[str, float] | None:
    provider_cost = _provider_cost(response)
    if provider_cost is not None:
        return {"total": provider_cost}
    price = get_llm_price(model)
    if price.name == "_unknown" or usage is None:
        return None
    input_cost = price.input_per_token * usage.get("input", 0) if price.billed else 0.0
    output_cost = price.output_per_token * usage.get("output", 0) if price.billed else 0.0
    total_cost = llm_cost_usd(model, usage.get("input", 0), usage.get("output", 0))
    return {"input": input_cost, "output": output_cost, "total": total_cost}


def embedding_cost_details(
    *, model: str, response: dict[str, Any], usage: dict[str, int] | None
) -> dict[str, float] | None:
    provider_cost = _provider_cost(response)
    if provider_cost is not None:
        return {"total": provider_cost}
    price = get_embedding_price(model)
    if price.name == "_unknown" or usage is None:
        return None
    total_cost = embedding_cost_usd(model, usage.get("input", 0))
    return {"input": total_cost, "total": total_cost}


def model_parameters(body: dict[str, Any]) -> dict[str, Any]:
    """Return only bounded, non-content model parameters accepted by Langfuse."""

    allowed = ("temperature", "max_tokens", "reasoning_control", "dimensions", "top_n")
    return {key: body[key] for key in allowed if key in body}


def _provider_cost(response: dict[str, Any]) -> float | None:
    usage = response.get("usage")
    hidden = response.get("_hidden_params")
    candidates = [
        usage.get("cost") if isinstance(usage, dict) else None,
        hidden.get("response_cost") if isinstance(hidden, dict) else None,
    ]
    for value in candidates:
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    return None


def _value_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(
            _value_chars(item.get("text") if isinstance(item, dict) else item) for item in value
        )
    return 0


def _preview(value: str) -> str:
    return value[:_PREVIEW_CHARS]


def _as_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "LangfuseObservation",
    "RequestTelemetry",
    "chat_cost_details",
    "chat_input",
    "chat_output",
    "chat_usage_details",
    "embedding_cost_details",
    "embedding_input",
    "embedding_output",
    "embedding_usage_details",
    "model_parameters",
    "rerank_input",
    "rerank_output",
]
