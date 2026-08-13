"""Payload privacy, accounting and fail-open Langfuse observation tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.llm import langfuse_observability as obs


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "APP_ENV": "prod",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
        "LANGFUSE_SECRET_KEY": "sk-lf-test",
        "LANGFUSE_CAPTURE_CONTENT": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class _RawObservation:
    def __init__(self, *, fail_update: bool = False, fail_end: bool = False) -> None:
        self.fail_update = fail_update
        self.fail_end = fail_end
        self.updates: list[dict[str, Any]] = []
        self.end_calls = 0

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)
        if self.fail_update:
            raise RuntimeError("export update failed")

    def end(self) -> None:
        self.end_calls += 1
        if self.fail_end:
            raise RuntimeError("export end failed")


class _Client:
    def __init__(self, *, raw: _RawObservation | None = None) -> None:
        self.raw = raw or _RawObservation()
        self.starts: list[dict[str, Any]] = []

    def start_observation(self, **kwargs: Any) -> _RawObservation:
        self.starts.append(kwargs)
        return self.raw


def test_observation_only_starts_under_active_parent(monkeypatch: Any) -> None:
    from app.agent import langfuse_handler

    inactive = _Client()

    def _unexpected_init(_settings: Any = None) -> None:
        raise AssertionError("calls outside a traced node must not initialize Langfuse")

    monkeypatch.setattr(langfuse_handler, "init_langfuse", _unexpected_init)
    monkeypatch.setattr(langfuse_handler, "current_langgraph_trace_context", lambda: None)
    skipped = obs.LangfuseObservation.start(
        settings=_settings(),
        name="litellm.chat",
        as_type="generation",
        input={"message_count": 1},
        model="mimo-v2.5",
    )
    skipped.finish_success(output={}, telemetry=obs.RequestTelemetry())
    assert inactive.starts == []

    active = _Client()
    monkeypatch.setattr(langfuse_handler, "init_langfuse", lambda _settings=None: active)
    monkeypatch.setattr(
        langfuse_handler,
        "current_langgraph_trace_context",
        lambda: {"trace_id": "ab" * 16, "parent_span_id": "cd" * 8},
    )
    observation = obs.LangfuseObservation.start(
        settings=_settings(),
        name="litellm.chat",
        as_type="generation",
        input={"message_count": 1},
        model="mimo-v2.5",
    )
    observation.finish_success(
        output={"content_chars": 2},
        telemetry=obs.RequestTelemetry(attempts=2, status_code=200),
        usage_details={"input": 10, "output": 2, "total": 12},
    )
    assert active.starts[0]["as_type"] == "generation"
    assert active.starts[0]["trace_context"] == {
        "trace_id": "ab" * 16,
        "parent_span_id": "cd" * 8,
    }
    assert active.raw.updates[0]["metadata"]["retry_count"] == 1
    assert active.raw.updates[0]["usage_details"]["total"] == 12
    assert active.raw.end_calls == 1


def test_observation_export_failures_never_escape(monkeypatch: Any) -> None:
    from app.agent import langfuse_handler

    raw = _RawObservation(fail_update=True, fail_end=True)
    client = _Client(raw=raw)
    monkeypatch.setattr(langfuse_handler, "init_langfuse", lambda _settings=None: client)
    monkeypatch.setattr(
        langfuse_handler,
        "current_langgraph_trace_context",
        lambda: {"trace_id": "ab" * 16, "parent_span_id": "cd" * 8},
    )
    observation = obs.LangfuseObservation.start(
        settings=_settings(), name="litellm.rerank", as_type="span", input={}
    )
    observation.finish_success(output={}, telemetry=obs.RequestTelemetry(attempts=1))
    observation.finish_error(RuntimeError("ignored"), telemetry=obs.RequestTelemetry())
    assert raw.end_calls == 1


def test_content_policy_summarizes_production_and_bounds_dev_previews() -> None:
    secret = "sensitive-query"
    prod = _settings(APP_ENV="prod", LANGFUSE_CAPTURE_CONTENT=False)
    chat = obs.chat_input(prod, [{"role": "user", "content": secret}])
    embedding = obs.embedding_input(prod, [secret])
    rerank = obs.rerank_input(prod, query=secret, documents=[secret], top_n=1)
    assert secret not in repr(chat) + repr(embedding) + repr(rerank)
    assert chat == {"message_count": 1, "roles": {"user": 1}, "content_chars": 15}
    assert embedding == {"input_count": 1, "input_chars": 15}
    assert rerank["query_chars"] == 15
    assert rerank["document_chars"] == [15]

    prod_capture = _settings(APP_ENV="prod", LANGFUSE_CAPTURE_CONTENT=True)
    assert "input_preview" not in obs.embedding_input(prod_capture, [secret])
    assert "query_preview" not in obs.rerank_input(
        prod_capture, query=secret, documents=[secret], top_n=1
    )

    dev_capture = _settings(APP_ENV="dev", LANGFUSE_CAPTURE_CONTENT=True)
    assert obs.embedding_input(dev_capture, [secret])["input_preview"] == [secret]
    assert obs.rerank_input(dev_capture, query=secret, documents=[secret], top_n=1)[
        "document_previews"
    ] == [secret]


def test_embedding_output_never_contains_vectors() -> None:
    response = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
        ]
    }
    output = obs.embedding_output(response)
    assert output == {"embedding_count": 2, "dimensions": [3], "indices": [0, 1]}
    assert "0.1" not in repr(output)


def test_usage_and_cost_use_provider_tokens_without_inventing_missing_usage() -> None:
    response = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "completion_tokens_details": {"reasoning_tokens": 5},
        }
    }
    usage = obs.chat_usage_details(response)
    assert usage == {"input": 100, "output": 20, "total": 120, "reasoning": 5}
    assert obs.chat_cost_details(
        model="mimo-v2.5", response=response, usage=usage
    ) == pytest.approx(
        {
            "input": 0.00004,
            "output": 0.00004,
            "total": 0.00008,
        }
    )
    assert obs.chat_usage_details({}) is None
    assert obs.chat_cost_details(model="mimo-v2.5", response={}, usage=None) is None


def test_provider_cost_takes_precedence_over_local_pricing() -> None:
    response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.123}}
    usage = obs.chat_usage_details(response)
    assert obs.chat_cost_details(model="mimo-v2.5", response=response, usage=usage) == {
        "total": 0.123
    }
