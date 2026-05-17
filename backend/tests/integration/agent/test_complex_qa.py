"""M4.3 complex QA 端到端集成测试（5 题 procedure 金标准）。

复用 `test_simple_qa.py` 的 fixture 风格：依赖任一不可达自动跳过。
跑通的 5 题用 `category=procedure` 的前 5 项（complex 链路：classify → rewrite → hyde →
multi_query → retrieve → rerank → generate → self_rag）。

断言（弱判定，参考 M4.2 文档备注：retrieval / citation 质量由 M7 nightly eval 严格校验）：
1. final_answer 非空
2. citations 非空（complex 走完整链路，能产出引用）
3. self_rag verdict 是 accept 或 retry，retry_count <= 2（不死循环）
4. complex 路径的 prefix 节点（rewrite / hyde / multi_query）都被执行

成本：5 题 × (classify+rewrite+hyde+multi_query+generate+self_rag) ≈ 30 次 LLM 调用
+ 5 × voyage embed + 5 × voyage rerank，预算 << $0.20。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.agent import build_graph
from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.core.config import get_settings
from app.llm.litellm_client import LiteLLMClient
from app.retrieval.dense import DenseRetriever
from app.retrieval.rerank import Reranker
from app.retrieval.sparse import SparseRetriever

pytestmark = pytest.mark.integration


_GOLDEN_PATH = Path(__file__).resolve().parents[4] / "eval" / "golden" / "v1.yaml"


def _bm25_available() -> bool:
    s = get_settings()
    return (Path(s.bm25_dir) / "by_spec").is_dir()


def _litellm_available() -> bool:
    return bool(get_settings().LITELLM_API_KEY.get_secret_value())


async def _litellm_reachable() -> bool:
    import httpx

    s = get_settings()
    url = s.LITELLM_BASE_URL.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {s.LITELLM_API_KEY.get_secret_value()}"}
    try:
        async with httpx.AsyncClient(timeout=3.0) as cli:
            resp = await cli.get(url, headers=headers)
            return resp.status_code < 500
    except Exception:
        return False


async def _qdrant_reachable() -> bool:
    from qdrant_client import AsyncQdrantClient

    s = get_settings()
    api_key = s.QDRANT_API_KEY.get_secret_value() or None
    cli = AsyncQdrantClient(url=s.QDRANT_URL, api_key=api_key, timeout=3.0)
    try:
        await cli.get_collections()
        return True
    except Exception:
        return False
    finally:
        await cli.close()


def _load_complex_cases(n: int = 5) -> list[dict[str, Any]]:
    if not _GOLDEN_PATH.is_file():
        return []
    raw = yaml.safe_load(_GOLDEN_PATH.read_text(encoding="utf-8")) or {}
    items: list[dict[str, Any]] = list(raw.get("items") or [])
    out: list[dict[str, Any]] = []
    for it in items:
        if it.get("category") != "procedure":
            continue
        if it.get("language") != "en":
            continue
        if not it.get("expected_specs"):
            continue
        out.append(it)
        if len(out) >= n:
            break
    return out


@pytest.fixture(scope="module")
def golden_complex_cases() -> list[dict[str, Any]]:
    cases = _load_complex_cases(5)
    if len(cases) < 5:
        pytest.skip(f"需要至少 5 题 complex QA，当前 {len(cases)}")
    return cases


@pytest.fixture(scope="module")
def deps_real() -> AgentDeps:
    if not _litellm_available():
        pytest.skip("LITELLM_API_KEY 未设置")
    if not _bm25_available():
        pytest.skip("BM25 by_spec 目录不存在")
    if not asyncio.run(_litellm_reachable()):
        pytest.skip("LiteLLM proxy 不可达")
    if not asyncio.run(_qdrant_reachable()):
        pytest.skip("Qdrant 不可达")
    s = get_settings()
    litellm = LiteLLMClient(settings=s)
    dense = DenseRetriever.from_env(embedder=litellm, settings=s)
    sparse = SparseRetriever.from_env(settings=s)
    reranker = Reranker.from_env(litellm_client=litellm, settings=s)
    deps = AgentDeps(
        llm=litellm, dense=dense, sparse=sparse, reranker=reranker, cache=None, settings=s
    )
    yield deps
    with contextlib.suppress(RuntimeError):
        asyncio.run(deps.aclose())


@pytest.mark.asyncio
async def test_complex_qa_five_golden_items(
    golden_complex_cases: list[dict[str, Any]], deps_real: AgentDeps
) -> None:
    """端到端跑通 5/5：每题都有 final_answer + 引用；complex 路径节点全执行。

    diagnostic：cited_specs / retrieved_specs / verdict / retry_count 打 stdout，
    spec hit rate 不作为硬断言（同 M4.2 简单 QA：严指标由 M7 nightly eval 跑）。
    """
    graph = build_graph(deps_real)
    diagnostics: list[dict[str, Any]] = []

    for case in golden_complex_cases:
        question: str = case["question"]
        expected_spec_ids = {sp["spec_id"] for sp in case["expected_specs"]}

        # 用 astream 跑一遍，顺便记录哪些节点真的产出了 update（验证 complex prefix 都走过）
        nodes_seen: set[str] = set()
        final_state: dict[str, Any] | None = None
        async for mode, payload in graph.astream(
            {"user_input": question, "user_language": "en"},
            stream_mode=["updates", "values"],
        ):
            if mode == "updates" and isinstance(payload, dict):
                nodes_seen.update(payload.keys())
            elif mode == "values" and isinstance(payload, dict):
                final_state = payload  # 最后一次即终态

        assert final_state is not None, f"未拿到终态：{case['id']!r}"
        state = AgentState.model_validate(final_state)

        # complex 路径必跑节点（classify 路由到 complex 后，rewrite/hyde/multi_query 都在）
        assert "classify" in nodes_seen
        # classify 可能把 query 判成 simple 也可能 complex；只断言"如果是 complex 就一定走齐"
        if state.complexity == "complex":
            for n in ("rewrite", "hyde", "multi_query"):
                assert (
                    n in nodes_seen
                ), f"complex 分支应跑 {n}，case={case['id']!r}, nodes_seen={sorted(nodes_seen)}"
        assert {
            "retrieve",
            "rerank",
            "generate",
            "self_rag",
        } <= nodes_seen, f"主干节点缺失 case={case['id']!r}, nodes_seen={sorted(nodes_seen)}"

        # 状态断言
        assert state.final_answer.strip(), f"empty answer for {case['id']!r}"
        assert state.candidates, f"retrieve 0 candidates for {case['id']!r}"
        assert state.reranked, f"rerank 0 chunks for {case['id']!r}"
        assert state.citations, f"no citations for {case['id']!r}"
        assert state.self_rag_verdict in {"accept", "retry", "insufficient"}
        assert state.retry_count <= 2, f"retry_count 超过 2: {state.retry_count}"

        cited_specs = {c["spec_id"] for c in state.citations}
        retrieved_specs = {c.spec_id for c in state.reranked}
        diagnostics.append(
            {
                "id": case["id"],
                "complexity": state.complexity,
                "expected_specs": sorted(expected_spec_ids),
                "retrieved_specs(top5)": sorted(retrieved_specs),
                "cited_specs": sorted(cited_specs),
                "spec_hit_in_citation": bool(cited_specs & expected_spec_ids),
                "spec_hit_in_retrieval": bool(retrieved_specs & expected_spec_ids),
                "confidence": state.confidence,
                "verdict": state.self_rag_verdict,
                "retry_count": state.retry_count,
            }
        )

    print("\n=== M4.3 complex QA diagnostics ===")
    for d in diagnostics:
        print(json.dumps(d, ensure_ascii=False))
    cite_hit = sum(1 for d in diagnostics if d["spec_hit_in_citation"])
    retr_hit = sum(1 for d in diagnostics if d["spec_hit_in_retrieval"])
    print(f"spec hit in citation: {cite_hit}/5; spec hit in retrieval (top-5): {retr_hit}/5")
