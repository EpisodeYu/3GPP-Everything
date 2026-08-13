"""Real Langfuse v4 callback contract against an in-memory OTLP exporter."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from langfuse import Langfuse
from langgraph.graph import END, START, StateGraph
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.agent import langfuse_handler
from app.agent.langfuse_handler import LangfuseRun
from app.core.config import Settings
from app.llm.litellm_client import LiteLLMClient

pytestmark = pytest.mark.integration


def _start_harness() -> tuple[Langfuse, InMemorySpanExporter, Settings]:
    exporter = InMemorySpanExporter()
    public_key = f"pk-lf-contract-{uuid.uuid4().hex}"
    client = Langfuse(
        public_key=public_key,
        secret_key="sk-lf-contract-test",
        environment="test",
        span_exporter=exporter,
    )
    langfuse_handler._reset_for_tests()
    langfuse_handler._client = client
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        APP_ENV="prod",
        LANGFUSE_PUBLIC_KEY=public_key,
        LANGFUSE_SECRET_KEY="sk-lf-contract-test",
        LANGFUSE_TRACING_ENVIRONMENT="test",
    )
    return client, exporter, settings


def _build_run(settings: Settings, run_id: str) -> LangfuseRun:
    run = langfuse_handler.build_langfuse_run(
        run_id=run_id,
        session_id=f"{run_id}-session",
        user_id="contract-user",
        message_id=f"{run_id}-message",
        mode="qa",
        settings=settings,
    )
    assert run is not None
    return run


def _config(run: LangfuseRun) -> dict[str, object]:
    return {
        "configurable": {"thread_id": run.metadata["langfuse_session_id"]},
        "run_name": "tgpp-agent",
        "callbacks": [run.handler],
        "metadata": run.metadata,
    }


def _spans_for_trace(exporter: InMemorySpanExporter, trace_id: str) -> list[object]:
    return [
        span
        for span in exporter.get_finished_spans()
        if format(span.context.trace_id, "032x") == trace_id
    ]


async def test_langgraph_callback_creates_root_and_node_spans_in_one_trace() -> None:
    client, exporter, settings = _start_harness()
    run = _build_run(settings, "contract-run")

    async def classify(_state: dict[str, object]) -> dict[str, object]:
        return {"query_class": "definition"}

    async def retrieve(_state: dict[str, object]) -> dict[str, object]:
        return {"candidate_count": 2}

    builder = StateGraph(dict)
    builder.add_node("classify", classify)
    builder.add_node("retrieve", retrieve)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", END)
    graph = builder.compile()

    try:
        async for _event in graph.astream_events(
            {"user_input": "What is AMF?"},
            config=_config(run),
            version="v2",
        ):
            pass
        client.flush()

        spans = exporter.get_finished_spans()
        by_name = {span.name: span for span in spans}
        assert set(by_name) == {"tgpp-agent", "classify", "retrieve"}
        assert run.handler.last_trace_id == run.trace_id
        assert {format(span.context.trace_id, "032x") for span in spans} == {run.trace_id}

        root = by_name["tgpp-agent"]
        assert by_name["classify"].parent is not None
        assert by_name["retrieve"].parent is not None
        assert by_name["classify"].parent.span_id == root.context.span_id
        assert by_name["retrieve"].parent.span_id == root.context.span_id
        assert root.attributes["langfuse.trace.name"] == "tgpp-chat"
        assert root.attributes["langfuse.trace.metadata.run_id"] == "contract-run"
    finally:
        langfuse_handler._reset_for_tests()
        client.shutdown()


async def test_langgraph_callback_records_only_executed_branch_and_repeated_nodes() -> None:
    client, exporter, settings = _start_harness()

    async def classify(state: dict[str, object]) -> dict[str, object]:
        return {"path": state["path"], "attempt": 0}

    async def simple(_state: dict[str, object]) -> dict[str, object]:
        return {"answer": "simple"}

    async def complex_path(_state: dict[str, object]) -> dict[str, object]:
        return {"answer": "draft"}

    async def self_rag(state: dict[str, object]) -> dict[str, object]:
        return {"attempt": int(state.get("attempt", 0)) + 1}

    builder = StateGraph(dict)
    builder.add_node("classify", classify)
    builder.add_node("simple", simple)
    builder.add_node("complex", complex_path)
    builder.add_node("self_rag", self_rag)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify", lambda state: str(state["path"]), {"simple": "simple", "complex": "complex"}
    )
    builder.add_edge("simple", END)
    builder.add_edge("complex", "self_rag")
    builder.add_conditional_edges(
        "self_rag",
        lambda state: "retry" if int(state["attempt"]) < 2 else "done",
        {"retry": "self_rag", "done": END},
    )
    graph = builder.compile()
    simple_run = _build_run(settings, "contract-simple")
    complex_run = _build_run(settings, "contract-complex")

    try:
        await graph.ainvoke({"path": "simple"}, config=_config(simple_run))
        await graph.ainvoke({"path": "complex"}, config=_config(complex_run))
        client.flush()

        simple_names = [span.name for span in _spans_for_trace(exporter, simple_run.trace_id)]
        complex_names = [span.name for span in _spans_for_trace(exporter, complex_run.trace_id)]
        # LangGraph also emits an SDK-internal ``<unknown>`` runnable for a
        # conditional-edge selector; assert the graph nodes independently.
        assert {"classify", "simple", "tgpp-agent"}.issubset(simple_names)
        assert "complex" not in simple_names
        assert "self_rag" not in simple_names
        assert "simple" not in complex_names
        assert complex_names.count("self_rag") == 2
        assert {"classify", "complex", "tgpp-agent"}.issubset(complex_names)
    finally:
        langfuse_handler._reset_for_tests()
        client.shutdown()


async def test_langgraph_callback_finishes_error_and_cancelled_spans() -> None:
    client, exporter, settings = _start_harness()

    async def explode(_state: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("contract failure")

    entered = asyncio.Event()

    async def wait_forever(_state: dict[str, object]) -> dict[str, object]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    error_builder = StateGraph(dict)
    error_builder.add_node("explode", explode)
    error_builder.add_edge(START, "explode")
    error_builder.add_edge("explode", END)
    error_graph = error_builder.compile()
    cancel_builder = StateGraph(dict)
    cancel_builder.add_node("wait_forever", wait_forever)
    cancel_builder.add_edge(START, "wait_forever")
    cancel_builder.add_edge("wait_forever", END)
    cancel_graph = cancel_builder.compile()
    error_run = _build_run(settings, "contract-error")
    cancel_run = _build_run(settings, "contract-cancel")

    try:
        with pytest.raises(RuntimeError, match="contract failure"):
            await error_graph.ainvoke({}, config=_config(error_run))

        task = asyncio.create_task(cancel_graph.ainvoke({}, config=_config(cancel_run)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.flush()

        assert {span.name for span in _spans_for_trace(exporter, error_run.trace_id)} == {
            "tgpp-agent",
            "explode",
        }
        assert {span.name for span in _spans_for_trace(exporter, cancel_run.trace_id)} == {
            "tgpp-agent",
            "wait_forever",
        }
    finally:
        langfuse_handler._reset_for_tests()
        client.shutdown()


async def test_litellm_observations_are_direct_children_of_executing_nodes() -> None:
    """Exercise the real Langfuse SDK, including concurrent embedding children."""

    client, exporter, settings = _start_harness()
    run = _build_run(settings, "contract-llm-observations")
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append({"path": request.url.path, "body": body})
        if request.url.path.endswith("/chat/completions") and body["stream"]:
            stream = (
                b'data: {"choices":[{"delta":{"content":"safe "}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"answer"},'
                b'"finish_reason":"stop"}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":9,'
                b'"completion_tokens":2,"total_tokens":11}}\n\n'
                b"data: [DONE]\n\n"
            )
            return httpx.Response(
                200, content=stream, headers={"content-type": "text/event-stream"}
            )
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "definition"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 2,
                        "total_tokens": 9,
                    },
                },
            )
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": [0.125, 0.25]}
                        for index, _value in enumerate(body["input"])
                    ],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
        if request.url.path.endswith("/rerank"):
            return httpx.Response(
                200,
                json={
                    "results": [{"index": 1, "relevance_score": 0.91}],
                    "usage": {"total_tokens": 14},
                },
            )
        raise AssertionError(f"unexpected test URL: {request.url}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = LiteLLMClient(settings=settings, client=http_client)

    async def classify(_state: dict[str, object]) -> dict[str, object]:
        await llm.chat(
            messages=[{"role": "user", "content": "private classify input"}],
            model="mimo-v2.5",
        )
        return {"query_class": "definition"}

    async def retrieve(_state: dict[str, object]) -> dict[str, object]:
        await asyncio.gather(
            llm.embed(["private embedding alpha"], model="voyage-4-large", dimensions=2),
            llm.embed(["private embedding beta"], model="voyage-4-large", dimensions=2),
        )
        return {"candidate_count": 2}

    async def rerank(_state: dict[str, object]) -> dict[str, object]:
        await llm.rerank(
            query="private rerank query",
            documents=["private document alpha", "private document beta"],
            model="rerank-2.5",
            top_k=1,
        )
        return {"reranked": True}

    async def generate(_state: dict[str, object]) -> dict[str, object]:
        chunks = [
            chunk
            async for chunk in llm.chat_stream(
                messages=[{"role": "user", "content": "private generation input"}],
                model="mimo-v2.5",
            )
        ]
        return {"chunk_count": len(chunks)}

    builder = StateGraph(dict)
    builder.add_node("classify", classify)
    builder.add_node("retrieve", retrieve)
    builder.add_node("rerank", rerank)
    builder.add_node("generate", generate)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "generate")
    builder.add_edge("generate", END)
    graph = builder.compile()

    try:
        await graph.ainvoke({"user_input": "private root input"}, config=_config(run))
        client.flush()

        spans = _spans_for_trace(exporter, run.trace_id)
        node_spans = {
            span.name: span
            for span in spans
            if span.name in {"classify", "retrieve", "rerank", "generate"}
        }
        llm_spans = [span for span in spans if span.name.startswith("litellm.")]
        by_llm_name: dict[str, list[Any]] = {}
        for span in llm_spans:
            by_llm_name.setdefault(span.name, []).append(span)

        assert {name: len(items) for name, items in by_llm_name.items()} == {
            "litellm.chat": 1,
            "litellm.embedding": 2,
            "litellm.rerank": 1,
            "litellm.chat_stream": 1,
        }
        expected_parent = {
            "litellm.chat": "classify",
            "litellm.embedding": "retrieve",
            "litellm.rerank": "rerank",
            "litellm.chat_stream": "generate",
        }
        for name, observations in by_llm_name.items():
            for observation in observations:
                assert observation.parent is not None
                assert (
                    observation.parent.span_id == node_spans[expected_parent[name]].context.span_id
                )

        assert by_llm_name["litellm.chat"][0].attributes["langfuse.observation.type"] == (
            "generation"
        )
        assert all(
            span.attributes["langfuse.observation.type"] == "embedding"
            for span in by_llm_name["litellm.embedding"]
        )
        assert by_llm_name["litellm.rerank"][0].attributes["langfuse.observation.type"] == ("span")
        stream_span = by_llm_name["litellm.chat_stream"][0]
        assert stream_span.attributes["langfuse.observation.type"] == "generation"
        assert "langfuse.observation.completion_start_time" in stream_span.attributes

        for span in [
            by_llm_name["litellm.chat"][0],
            *by_llm_name["litellm.embedding"],
            stream_span,
        ]:
            assert "langfuse.observation.usage_details" in span.attributes
            assert "langfuse.observation.cost_details" in span.attributes

        serialized_attributes = json.dumps(
            {span.name: dict(span.attributes) for span in llm_spans},
            ensure_ascii=False,
        )
        assert "private classify input" not in serialized_attributes
        assert "private embedding" not in serialized_attributes
        assert "private rerank query" not in serialized_attributes
        assert "private document" not in serialized_attributes
        assert "private generation input" not in serialized_attributes
        assert "0.125" not in serialized_attributes
        assert all(
            request["body"].get("stream_options") == {"include_usage": True}
            for request in requests
            if request["path"].endswith("/chat/completions") and request["body"]["stream"]
        )
    finally:
        await http_client.aclose()
        langfuse_handler._reset_for_tests()
        client.shutdown()
