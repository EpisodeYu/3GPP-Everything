"""Langfuse request factory, masking and lifecycle tests."""

from __future__ import annotations

import hashlib
from typing import Any

from app.agent import langfuse_handler
from app.core.config import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "APP_ENV": "prod",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
        "LANGFUSE_SECRET_KEY": "sk-lf-test",
        "LANGFUSE_HOST": "https://langfuse.example.test",
        "LANGFUSE_TRACING_ENABLED": True,
        "LANGFUSE_TRACING_ENVIRONMENT": "production",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class _FakeClient:
    def __init__(self) -> None:
        self.seeds: list[str | None] = []
        self.shutdown_calls = 0

    def create_trace_id(self, *, seed: str | None = None) -> str:
        self.seeds.append(seed)
        return hashlib.md5((seed or "random").encode(), usedforsecurity=False).hexdigest()

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeHandler:
    def __init__(
        self, *, public_key: str | None = None, trace_context: dict[str, str] | None = None
    ) -> None:
        self.public_key = public_key
        self.trace_context = trace_context


def _patch_sdk(monkeypatch: Any, client: _FakeClient) -> None:
    from langfuse import langchain as langfuse_langchain

    monkeypatch.setattr(langfuse_handler, "init_langfuse", lambda _settings=None: client)
    monkeypatch.setattr(langfuse_langchain, "CallbackHandler", _FakeHandler)


def test_init_langfuse_passes_v4_runtime_configuration(monkeypatch: Any) -> None:
    import langfuse

    captured: dict[str, Any] = {}
    fake_client = _FakeClient()

    def _fake_langfuse(**kwargs: Any) -> _FakeClient:
        captured.update(kwargs)
        return fake_client

    langfuse_handler._reset_for_tests()
    monkeypatch.setattr(langfuse, "Langfuse", _fake_langfuse)
    settings = _settings(
        LANGFUSE_SAMPLE_RATE=0.25,
        LANGFUSE_RELEASE="release-sha",
        LANGFUSE_CAPTURE_CONTENT=False,
    )

    assert langfuse_handler.init_langfuse(settings) is fake_client
    assert captured["base_url"] == "https://langfuse.example.test"
    assert captured["tracing_enabled"] is True
    assert captured["sample_rate"] == 0.25
    assert captured["environment"] == "production"
    assert captured["release"] == "release-sha"
    assert captured["mask"](data={"user_input": "private"}) == {"user_input": "<masked:7 chars>"}
    langfuse_handler._reset_for_tests()


def test_init_langfuse_respects_explicit_kill_switch(monkeypatch: Any) -> None:
    import langfuse

    def _unexpected_client(**_kwargs: Any) -> None:
        raise AssertionError("Langfuse client must not be initialized")

    langfuse_handler._reset_for_tests()
    monkeypatch.setattr(langfuse, "Langfuse", _unexpected_client)
    assert langfuse_handler.init_langfuse(_settings(LANGFUSE_TRACING_ENABLED=False)) is None
    langfuse_handler._reset_for_tests()


def test_build_langfuse_run_creates_stable_trace_and_fresh_handler(monkeypatch: Any) -> None:
    client = _FakeClient()
    _patch_sdk(monkeypatch, client)
    settings = _settings()

    kwargs = {
        "run_id": "run-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "message_id": "message-1",
        "mode": "qa",
        "settings": settings,
    }
    first = langfuse_handler.build_langfuse_run(**kwargs)
    second = langfuse_handler.build_langfuse_run(**kwargs)

    assert first is not None and second is not None
    assert first.trace_id == second.trace_id
    assert first.handler is not second.handler
    assert first.handler.public_key == "pk-lf-test"
    assert first.handler.trace_context == {"trace_id": first.trace_id}
    assert client.seeds == ["tgpp:run-1", "tgpp:run-1"]
    assert first.metadata == {
        "app": "tgpp",
        "langfuse_session_id": "session-1",
        "langfuse_user_id": "user-1",
        "langfuse_trace_name": "tgpp-chat",
        "langfuse_tags": ["langgraph", "production", "qa"],
        "mode": "qa",
        "run_id": "run-1",
        "message_id": "message-1",
    }


def test_build_langfuse_run_reuses_valid_trace_id(monkeypatch: Any) -> None:
    client = _FakeClient()
    _patch_sdk(monkeypatch, client)
    stored_trace_id = "ab" * 16

    run = langfuse_handler.build_langfuse_run(
        run_id="run-resume",
        session_id="session-1",
        user_id="user-1",
        message_id="message-1",
        mode="qa",
        trace_id=stored_trace_id,
        settings=_settings(),
    )

    assert run is not None
    assert run.trace_id == stored_trace_id
    assert client.seeds == []


def test_build_langfuse_run_replaces_invalid_stored_trace_id(monkeypatch: Any) -> None:
    client = _FakeClient()
    _patch_sdk(monkeypatch, client)

    run = langfuse_handler.build_langfuse_run(
        run_id="run-resume",
        session_id="session-1",
        user_id="user-1",
        message_id="message-1",
        mode="qa",
        trace_id="not-a-w3c-trace-id",
        settings=_settings(),
    )

    assert run is not None
    expected = hashlib.md5(b"tgpp:run-resume", usedforsecurity=False).hexdigest()
    assert run.trace_id == expected
    assert client.seeds == ["tgpp:run-resume"]


def test_mask_trace_data_redacts_content_secrets_and_large_chunk_payloads() -> None:
    masked = langfuse_handler.mask_trace_data(
        data={
            "user_input": "private question",
            "final_answer": "private answer",
            "raw_history": [{"role": "user", "content": "older private question"}],
            "authorization": "Bearer secret",
            "bearer_token": "secret-token",
            "prompt_tokens": 12,
            "candidates": [
                {
                    "chunk_id": "chunk-1",
                    "spec_id": "23.501",
                    "section_path": "5.2",
                    "content": "x" * 10000,
                    "score_fused": 0.9,
                }
            ],
        }
    )

    assert masked["user_input"] == "<masked:16 chars>"
    assert masked["final_answer"] == "<masked:14 chars>"
    assert masked["raw_history"] == {
        "masked": True,
        "message_count": 1,
        "content_chars": 22,
        "roles": {"user": 1},
    }
    assert masked["authorization"] == "<masked>"
    assert masked["bearer_token"] == "<masked>"
    assert masked["prompt_tokens"] == 12
    assert masked["candidates"] == [
        {
            "chunk_id": "chunk-1",
            "spec_id": "23.501",
            "section_path": "5.2",
            "score_fused": 0.9,
            "content_chars": 10000,
        }
    ]


def test_mask_trace_data_hides_candidate_query_text_by_default() -> None:
    masked = langfuse_handler.mask_trace_data(
        data={
            "candidates_by_query": {
                "private rewritten question": [
                    {"chunk_id": "chunk-1", "content": "retrieved private content"}
                ]
            }
        }
    )

    assert masked["candidates_by_query"] == {
        "query_1": [{"chunk_id": "chunk-1", "content_chars": 25}]
    }


def test_mask_trace_data_hides_derived_queries_and_compacts_nested_candidate_pools() -> None:
    masked = langfuse_handler.mask_trace_data(
        data={
            "contextualized_input": "private standalone question",
            "rewritten_queries": ["private facet one", "private facet two"],
            "self_rag_missing": ["private missing fact"],
            "tool_results": {"web": "private web result"},
            "candidates_by_query": [
                [
                    {
                        "chunk_id": "chunk-1",
                        "spec_id": "23.501",
                        "content": "private retrieved content",
                        "fused_score": 0.8,
                        "score_rerank": 0.9,
                    }
                ]
            ],
        }
    )

    assert masked["contextualized_input"] == "<masked:27 chars>"
    assert masked["rewritten_queries"] == {"masked": True, "item_count": 2}
    assert masked["self_rag_missing"] == {"masked": True, "item_count": 1}
    assert masked["tool_results"] == {"masked": True, "field_count": 1}
    assert masked["candidates_by_query"] == [
        [
            {
                "chunk_id": "chunk-1",
                "spec_id": "23.501",
                "fused_score": 0.8,
                "score_rerank": 0.9,
                "content_chars": 25,
            }
        ]
    ]


def test_mask_trace_data_capture_content_still_truncates_large_strings() -> None:
    masked = langfuse_handler.mask_trace_data(data={"user_input": "x" * 5000}, capture_content=True)

    assert masked["user_input"].startswith("x" * 4000)
    assert masked["user_input"].endswith("<truncated:1000 chars>")


def test_shutdown_langfuse_is_idempotent() -> None:
    client = _FakeClient()
    langfuse_handler._reset_for_tests()
    langfuse_handler._client = client

    langfuse_handler.shutdown_langfuse()
    langfuse_handler.shutdown_langfuse()

    assert client.shutdown_calls == 1
    assert langfuse_handler._client is None
