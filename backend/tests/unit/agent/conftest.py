"""Agent 单测共用 stub：StubLLM / StubDense / StubSparse / StubReranker / build_deps。

设计：
- 节点签名是 `state, *, deps`；测试直接 `await node(state, deps=stub_deps)` 即可
- StubLLM 支持按调用次数排队不同响应（`StubLLM(responses=[r1, r2, r3])`）
- StubDense / StubSparse 返回 `RetrievalChunk`（与生产 retriever 一致），节点内部
  会转成 `state.RetrievedChunk`
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from app.agent.deps import AgentDeps
from app.core.config import Settings
from app.retrieval.models import RetrievedChunk as RetrievalChunk


@dataclass
class StubLLM:
    """OpenAI 兼容 chat / embed / rerank stub。

    `responses` 按调用顺序消费；不够时复用最后一条。
    单次调用记录在 `calls` 列表，便于断言 prompt 内容。
    """

    responses: list[str | dict[str, Any]]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _next_content(self, kind: str, messages: Sequence[dict[str, Any]], **kwargs: Any) -> str:
        """共享 responses 队列：chat / chat_stream 按调用顺序消费同一份列表。

        idx 按所有 chat-like calls 的总数算（chat + chat_stream），避免 graph 顺序
        classify(chat) → generate(chat_stream) → self_rag(chat) 时 idx 错位。
        """
        self.calls.append({"kind": kind, "messages": list(messages), **kwargs})
        chat_like = [c for c in self.calls if c["kind"] in ("chat", "chat_stream")]
        idx = min(len(chat_like) - 1, len(self.responses) - 1)
        item = self.responses[idx] if self.responses else ""
        return (item.get("content") or json.dumps(item)) if isinstance(item, dict) else item

    async def chat(self, messages: Sequence[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        content = self._next_content("chat", messages, **kwargs)
        return {"choices": [{"message": {"content": content}}]}

    async def chat_stream(self, messages: Sequence[dict[str, Any]], **kwargs: Any) -> Any:
        """模拟 OpenAI 兼容流式 chat：把对应 response 拆成 ~3 段 yield。"""
        content = self._next_content("chat_stream", messages, **kwargs)
        if not content:
            return
        n = len(content)
        slices = [content[: n // 3], content[n // 3 : 2 * n // 3], content[2 * n // 3 :]]
        for s in slices:
            if not s:
                continue
            yield {"choices": [{"delta": {"content": s}}]}

    async def embed(self, inputs: Sequence[str], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"kind": "embed", "inputs": list(inputs), **kwargs})
        return {"data": [{"index": 0, "embedding": [0.0] * 8}]}

    async def rerank(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"kind": "rerank", **kwargs})
        return []

    async def close(self) -> None:
        pass


@dataclass
class StubDense:
    """async dense retriever stub。"""

    chunks: list[RetrievalChunk]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 30,
        filter_spec_ids: Sequence[str] | None = None,
    ) -> list[RetrievalChunk]:
        self.calls.append(
            {"query": query, "top_k": top_k, "filter_spec_ids": list(filter_spec_ids or [])}
        )
        return list(self.chunks)[:top_k]

    async def close(self) -> None:
        pass


@dataclass
class StubSparse:
    """sync sparse retriever stub（在节点里被 to_thread 调）。"""

    chunks: list[RetrievalChunk]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, query: str, *, top_k: int = 30) -> list[RetrievalChunk]:
        self.calls.append({"query": query, "top_k": top_k})
        return list(self.chunks)[:top_k]


@dataclass
class StubReranker:
    """rerank stub：按 score_rerank 列表重排候选。"""

    scores: list[float]

    def __post_init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def rerank(
        self, query: str, candidates: list[RetrievalChunk], *, top_k: int = 5
    ) -> list[RetrievalChunk]:
        self.calls.append({"query": query, "n": len(candidates), "top_k": top_k})
        scores = self.scores or [0.0] * len(candidates)
        scored = [(s, c) for s, c in zip(scores[: len(candidates)], candidates, strict=False)]
        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[RetrievalChunk] = []
        for s, c in scored[:top_k]:
            out.append(
                RetrievalChunk(
                    chunk_id=c.chunk_id,
                    spec_id=c.spec_id,
                    section_path=c.section_path,
                    section_title=c.section_title,
                    chunk_type=c.chunk_type,
                    content=c.content,
                    score_dense=c.score_dense,
                    score_sparse=c.score_sparse,
                    score_rerank=float(s),
                    fused_score=c.fused_score,
                    extra=dict(c.extra),
                )
            )
        return out


def make_chunk(
    chunk_id: str,
    *,
    spec_id: str = "38.331",
    section: tuple[str, ...] = ("5", "3"),
    title: str = "RRC connection establishment",
    chunk_type: str = "text",
    content: str = "",
    score_dense: float | None = None,
    score_sparse: float | None = None,
) -> RetrievalChunk:
    return RetrievalChunk(
        chunk_id=chunk_id,
        spec_id=spec_id,
        section_path=section,
        section_title=title,
        chunk_type=chunk_type,
        content=content or f"content for {chunk_id}",
        score_dense=score_dense,
        score_sparse=score_sparse,
    )


def make_settings(**overrides: Any) -> Settings:
    base = dict(
        APP_ENV="dev",
        LITELLM_API_KEY="test-key",
        QDRANT_URL="http://localhost:6333",
        EMBEDDING_DIMENSIONS=8,
        VOYAGE_OUTPUT_DIMENSION=8,
        RETRIEVAL_DENSE_TOP_K=10,
        RETRIEVAL_SPARSE_TOP_K=10,
        RETRIEVAL_FINAL_TOP_K=20,
        RERANK_TOP_K=3,
    )
    base.update(overrides)
    return Settings(**base)


def make_deps(
    *,
    llm: StubLLM | None = None,
    dense: StubDense | None = None,
    sparse: StubSparse | None = None,
    reranker: StubReranker | None = None,
    settings: Settings | None = None,
) -> AgentDeps:
    return AgentDeps(
        llm=llm or StubLLM(responses=[""]),  # type: ignore[arg-type]
        dense=dense or StubDense(chunks=[]),
        sparse=sparse,
        reranker=reranker,
        cache=None,
        settings=settings or make_settings(),
    )


# fixtures ----


@pytest.fixture
def stub_llm() -> StubLLM:
    return StubLLM(responses=[""])


@pytest.fixture
def stub_dense() -> StubDense:
    return StubDense(chunks=[])


@pytest.fixture
def stub_sparse() -> StubSparse:
    return StubSparse(chunks=[])


@pytest.fixture
def stub_reranker() -> StubReranker:
    return StubReranker(scores=[])
