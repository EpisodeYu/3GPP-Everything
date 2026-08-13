"""Langfuse v4 tracing integration.

The Langfuse client is process-scoped, while ``CallbackHandler`` is run-scoped:
the handler keeps mutable LangChain run state and must never be shared by concurrent
graph invocations. Missing configuration or any SDK failure disables tracing without
affecting the agent path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, is_dataclass
from threading import Lock
from typing import Any

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

_client_lock = Lock()
_client: Any | None = None
_client_init_failed = False

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_STRING_CHARS = 4000
_MAX_LIST_ITEMS = 50
_MAX_DICT_ITEMS = 100
_MAX_DEPTH = 8
_CONTENT_KEYS = {
    "content",
    "contextualized_input",
    "final_answer",
    "hyde_doc",
    "messages",
    "history",
    "prompt",
    "prompts",
    "queries",
    "query",
    "rewritten_queries",
    "rewritten_query",
    "self_rag_missing",
    "tool_results",
    "user_input",
}
_CHUNK_LIST_KEYS = {"candidates", "reranked"}
_CHUNK_FIELDS = (
    "chunk_id",
    "spec_id",
    "section_path",
    "section_title",
    "score_dense",
    "score_sparse",
    "score_fused",
    "fused_score",
    "rerank_score",
    "score_rerank",
)


@dataclass(slots=True, frozen=True)
class LangfuseRun:
    """Trace data that belongs to one graph invocation."""

    trace_id: str
    handler: Any
    metadata: dict[str, Any]


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    if "password" in normalized or "secret" in normalized or "authorization" in normalized:
        return True
    if normalized == "token" or normalized.endswith("_token"):
        return True
    if normalized == "api_key":
        return True
    return normalized.endswith("_api_key")


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="python")
            return dumped if isinstance(dumped, dict) else None
        except Exception:
            return None
    if is_dataclass(value) and not isinstance(value, type):
        try:
            dumped = asdict(value)
            return dumped if isinstance(dumped, dict) else None
        except Exception:
            return None
    return None


def _masked_summary(value: Any) -> str | dict[str, Any]:
    if isinstance(value, str):
        return f"<masked:{len(value)} chars>"
    if isinstance(value, (list, tuple)):
        return {"masked": True, "item_count": len(value)}
    mapping = _as_mapping(value)
    if mapping is not None:
        return {"masked": True, "field_count": len(mapping)}
    return "<masked>"


def _compact_chunk(value: Any) -> dict[str, Any]:
    mapping = _as_mapping(value) or {}
    compact = {field: mapping[field] for field in _CHUNK_FIELDS if mapping.get(field) is not None}
    content = mapping.get("expanded_content") or mapping.get("content")
    if isinstance(content, str):
        compact["content_chars"] = len(content)
    return compact or {"masked": True}


def _compact_history(value: Any) -> dict[str, Any] | str:
    """Keep useful history cardinality without exporting conversation text."""

    if not isinstance(value, (list, tuple)):
        return _masked_summary(value)
    role_counts: dict[str, int] = {}
    content_chars = 0
    for item in value:
        mapping = _as_mapping(item)
        if mapping is None:
            continue
        role = str(mapping.get("role") or "unknown")[:40]
        role_counts[role] = role_counts.get(role, 0) + 1
        content = mapping.get("content")
        if isinstance(content, str):
            content_chars += len(content)
    return {
        "masked": True,
        "message_count": len(value),
        "content_chars": content_chars,
        "roles": role_counts,
    }


def _mask_value(
    value: Any,
    *,
    capture_content: bool,
    key: str | None = None,
    depth: int = 0,
) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "<masked>"
    if depth >= _MAX_DEPTH:
        return "<max-depth>"

    normalized_key = (key or "").lower()
    if not capture_content and normalized_key == "raw_history":
        return _compact_history(value)
    if normalized_key == "candidates_by_query" and isinstance(value, (list, tuple)):
        return [
            [_compact_chunk(item) for item in chunks[:_MAX_LIST_ITEMS]]
            for chunks in value[:_MAX_LIST_ITEMS]
            if isinstance(chunks, (list, tuple))
        ]
    if normalized_key in _CHUNK_LIST_KEYS and isinstance(value, (list, tuple)):
        return [_compact_chunk(item) for item in value[:_MAX_LIST_ITEMS]]
    if normalized_key == "candidates_by_query" and isinstance(value, dict):
        return {
            (str(query)[:200] if capture_content else f"query_{index}"): [
                _compact_chunk(item) for item in chunks[:_MAX_LIST_ITEMS]
            ]
            for index, (query, chunks) in enumerate(list(value.items())[:_MAX_DICT_ITEMS], start=1)
            if isinstance(chunks, (list, tuple))
        }
    if not capture_content and normalized_key in _CONTENT_KEYS:
        return _masked_summary(value)

    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return f"{value[:_MAX_STRING_CHARS]}…<truncated:{len(value) - _MAX_STRING_CHARS} chars>"
    if value is None or isinstance(value, (bool, int, float)):
        return value

    mapping = _as_mapping(value)
    if mapping is not None:
        masked: dict[str, Any] = {}
        for item_key, item_value in list(mapping.items())[:_MAX_DICT_ITEMS]:
            item_key_str = str(item_key)
            masked[item_key_str] = _mask_value(
                item_value,
                capture_content=capture_content,
                key=item_key_str,
                depth=depth + 1,
            )
        if len(mapping) > _MAX_DICT_ITEMS:
            masked["_truncated_fields"] = len(mapping) - _MAX_DICT_ITEMS
        return masked

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        masked_items = [
            _mask_value(item, capture_content=capture_content, depth=depth + 1)
            for item in items[:_MAX_LIST_ITEMS]
        ]
        if len(items) > _MAX_LIST_ITEMS:
            masked_items.append({"_truncated_items": len(items) - _MAX_LIST_ITEMS})
        return masked_items

    return value


def mask_trace_data(*, data: Any, capture_content: bool = False, **_kwargs: Any) -> Any:
    """Langfuse mask hook: redact secrets and bound state-heavy payloads."""

    return _mask_value(data, capture_content=capture_content)


def init_langfuse(settings: Settings | None = None) -> Any | None:
    """Lazily initialize the process-scoped Langfuse client."""

    global _client, _client_init_failed
    if _client is not None:
        return _client
    if _client_init_failed:
        return None

    with _client_lock:
        if _client is not None:
            return _client
        if _client_init_failed:
            return None
        s = settings or get_settings()
        pk = s.LANGFUSE_PUBLIC_KEY.get_secret_value().strip()
        sk = s.LANGFUSE_SECRET_KEY.get_secret_value().strip()
        base_url = s.LANGFUSE_HOST.strip()
        if not s.LANGFUSE_TRACING_ENABLED:
            log.info("langfuse tracing disabled by configuration")
            _client_init_failed = True
            return None
        if not pk or not sk:
            log.info("langfuse keys missing, tracing disabled")
            _client_init_failed = True
            return None
        try:
            from langfuse import Langfuse

            _client = Langfuse(
                public_key=pk,
                secret_key=sk,
                base_url=base_url,
                tracing_enabled=True,
                sample_rate=s.LANGFUSE_SAMPLE_RATE,
                environment=s.LANGFUSE_TRACING_ENVIRONMENT.strip() or s.APP_ENV,
                release=s.LANGFUSE_RELEASE.strip() or None,
                mask=lambda *, data, **kwargs: mask_trace_data(
                    data=data,
                    capture_content=s.LANGFUSE_CAPTURE_CONTENT,
                    **kwargs,
                ),
            )
            return _client
        except Exception as exc:
            log.warning("langfuse client init failed: %s", exc)
            _client_init_failed = True
            return None


def build_callback_handler(settings: Settings | None = None) -> Any | None:
    """Build a fresh handler for callers that do not need a predefined trace ID."""

    s = settings or get_settings()
    if init_langfuse(s) is None:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler(public_key=s.LANGFUSE_PUBLIC_KEY.get_secret_value().strip())
    except Exception as exc:
        log.warning("langfuse CallbackHandler unavailable: %s", exc)
        return None


def build_trace_metadata(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    mode: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build LangChain metadata using Langfuse v4's reserved keys."""

    meta: dict[str, Any] = {"app": "tgpp"}
    if session_id is not None:
        meta["langfuse_session_id"] = session_id
    if user_id is not None:
        meta["langfuse_user_id"] = user_id
    if trace_name is not None:
        meta["langfuse_trace_name"] = trace_name
    if tags:
        meta["langfuse_tags"] = tags
    if mode is not None:
        meta["mode"] = mode
    if extra:
        meta.update(extra)
    return meta


def build_langfuse_run(
    *,
    run_id: str,
    session_id: str,
    user_id: str,
    message_id: str,
    mode: str,
    trace_id: str | None = None,
    settings: Settings | None = None,
) -> LangfuseRun | None:
    """Create a stable trace ID and a fresh callback handler for one graph invocation."""

    s = settings or get_settings()
    client = init_langfuse(s)
    if client is None:
        return None
    try:
        effective_trace_id = (trace_id or "").strip().lower()
        if not _TRACE_ID_RE.fullmatch(effective_trace_id):
            if effective_trace_id:
                log.warning("invalid stored langfuse trace id; deriving from run_id")
            effective_trace_id = client.create_trace_id(seed=f"tgpp:{run_id}")

        from langfuse.langchain import CallbackHandler

        handler = CallbackHandler(
            public_key=s.LANGFUSE_PUBLIC_KEY.get_secret_value().strip(),
            trace_context={"trace_id": effective_trace_id},
        )
        environment = s.LANGFUSE_TRACING_ENVIRONMENT.strip() or s.APP_ENV
        metadata = build_trace_metadata(
            session_id=session_id,
            user_id=user_id,
            mode=mode,
            trace_name="tgpp-chat",
            tags=["langgraph", environment, mode],
            extra={"run_id": run_id, "message_id": message_id},
        )
        return LangfuseRun(
            trace_id=effective_trace_id,
            handler=handler,
            metadata=metadata,
        )
    except Exception as exc:
        log.warning("langfuse run tracing unavailable: %s", exc)
        return None


def shutdown_langfuse() -> None:
    """Flush and stop the exporter once during application shutdown."""

    global _client, _client_init_failed
    with _client_lock:
        client = _client
        _client = None
        _client_init_failed = False
    if client is None:
        return
    try:
        client.shutdown()
    except Exception as exc:
        log.warning("langfuse shutdown failed: %s", exc)


def _reset_for_tests() -> None:
    """Reset singleton state without exporting data; tests own fake-client lifecycle."""

    global _client, _client_init_failed
    with _client_lock:
        _client = None
        _client_init_failed = False
