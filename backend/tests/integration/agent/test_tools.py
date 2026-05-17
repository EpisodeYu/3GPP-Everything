"""M4.4 工具节点集成测：4 个工具显式触发 + 非 explicit_tools 不触发。

口径：`docs/03-development/03-agent.md §14 M4.4` + §4.9。

四条断言（与 §14 M4.4 三个 `[auto]` checkbox 对齐 + 真实 glossary 命中加一条）：
1. test_each_tool_triggered_via_explicit_tools_*: 每个工具单独跑一遍，验证写入
   `state.tool_results[<name>]` + tool_dispatch 节点确实被 graph 调用
2. test_glossary_tool_hits_real_data: 用真实 PG `glossary` 表查 AMF，验证返回非空
   matches（依赖 M4.1，需要 .env LITELLM_API_KEY + PG 可达）
3. test_non_explicit_tools_does_not_invoke_tools: 用 "definition" 类查询、
   explicit_tools=[]，验证 graph 不进 tool_dispatch（tool_dispatch_node 不被调用
   即可，因为 classify→retrieve→...→generate 那条路径上压根没走 tool_dispatch）

实现策略：
- 各工具用 mock（StubLLM / StubDense / StubSparse / 注入 FakeSessionmaker），
  避免 100+ DB 调用 + 真实 Tavily 触发成本
- 真实 glossary 命中那条单独跑，依赖任一不可达自动跳过
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.agent import build_graph
from app.agent.deps import AgentDeps
from app.agent.state import AgentState
from app.core.config import get_settings
from app.llm.litellm_client import LiteLLMClient
from app.tools import web_search as web_search_mod

from ...unit.agent.conftest import (
    StubDense,
    StubLLM,
    StubReranker,
    StubSparse,
    make_chunk,
    make_deps,
)
from ...unit.tools.conftest import FakeSessionmaker

pytestmark = pytest.mark.integration


def _tool_classify_resp(tools: list[str]) -> str:
    return json.dumps(
        {
            "query_class": "tool",
            "complexity": "simple",
            "detected_language": "en",
            "rewritten_query": "tool query",
            "needs_explicit_tools": tools,
            "reason": "explicit tool",
        }
    )


def _self_rag_accept_resp() -> str:
    return json.dumps(
        {
            "faithful": True,
            "coverage": 0.8,
            "confidence": 0.8,
            "verdict": "accept",
            "missing_aspects": [],
        }
    )


# ---- 1. 4 个工具各跑一次 -------------------------------------------------


async def test_glossary_triggered_via_explicit_tools() -> None:
    row = SimpleNamespace(
        term="AMF",
        normalized_term="amf",
        definition="Access and Mobility Management Function.",
        spec_id="23.501",
        section_path=["3", "1"],
    )
    llm = StubLLM(responses=[_tool_classify_resp(["glossary"]), _self_rag_accept_resp()])
    deps = make_deps(llm=llm)
    deps.db_sessionmaker = FakeSessionmaker(rows=[row])  # type: ignore[assignment]
    graph = build_graph(deps)

    out = await graph.ainvoke({"user_input": "What is AMF?", "explicit_tools": ["glossary"]})
    state = AgentState.model_validate(out)
    assert "glossary" in state.tool_results
    assert state.tool_results["glossary"]["matches"][0]["term"] == "AMF"
    # final_answer 由 _render_tool_results 落地
    assert "AMF" in state.final_answer


async def test_toc_triggered_via_explicit_tools() -> None:
    rows = [
        SimpleNamespace(
            section_path=["5", "3"],
            section_title="RRC connection establishment",
            chunk_id="c1",
            chunk_type="text",
        )
    ]
    llm = StubLLM(responses=[_tool_classify_resp(["toc"]), _self_rag_accept_resp()])
    deps = make_deps(llm=llm)
    deps.db_sessionmaker = FakeSessionmaker(rows=rows)  # type: ignore[assignment]
    graph = build_graph(deps)

    out = await graph.ainvoke(
        {"user_input": "list 38.331 §5.3 subsections", "explicit_tools": ["toc"]}
    )
    state = AgentState.model_validate(out)
    assert "toc" in state.tool_results
    assert state.tool_results["toc"]["spec_id"] == "38.331"
    assert state.tool_results["toc"]["items"]
    assert "38.331" in state.final_answer


async def test_params_triggered_via_explicit_tools() -> None:
    chunks = [
        make_chunk(
            "c1",
            spec_id="38.331",
            section=("9", "1"),
            chunk_type="table",
            content="RSRP threshold field appears in this table.",
            score_sparse=0.7,
        ),
        make_chunk("c2", chunk_type="figure"),  # 应被 params 过滤掉
    ]
    llm = StubLLM(responses=[_tool_classify_resp(["params"]), _self_rag_accept_resp()])
    sparse = StubSparse(chunks=chunks)
    deps = make_deps(llm=llm, sparse=sparse)
    graph = build_graph(deps)

    out = await graph.ainvoke(
        {"user_input": "where does RSRP threshold field appear", "explicit_tools": ["params"]}
    )
    state = AgentState.model_validate(out)
    assert "params" in state.tool_results
    hits = state.tool_results["params"]["hits"]
    assert hits and hits[0]["chunk_id"] == "c1"
    assert all(h["chunk_type"] in {"text", "table"} for h in hits)


async def test_web_search_triggered_via_explicit_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stub:
        def search(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "results": [
                    {
                        "title": "3GPP TS 38.331 v18 release notes",
                        "url": "https://example.com/3gpp",
                        "content": "Latest release of NR RRC ...",
                    }
                ]
            }

    monkeypatch.setattr(web_search_mod, "_build_client", lambda key: _Stub())

    llm = StubLLM(responses=[_tool_classify_resp(["web_search"]), _self_rag_accept_resp()])
    deps = make_deps(llm=llm)
    deps.settings = deps.settings.model_copy(update={"TAVILY_API_KEY": SecretStr("test-key")})
    graph = build_graph(deps)

    out = await graph.ainvoke(
        {
            "user_input": "search the web for latest 38.331 v18 release",
            "explicit_tools": ["web_search"],
        }
    )
    state = AgentState.model_validate(out)
    assert "web_search" in state.tool_results
    results = state.tool_results["web_search"]["results"]
    assert results and results[0]["url"] == "https://example.com/3gpp"
    # §4.9 强制前缀
    assert state.final_answer.startswith("以下内容来自 Web 搜索")


# ---- 2. 非 explicit_tools 不触发工具 ---------------------------------------


async def test_non_explicit_tools_does_not_invoke_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """definition 类查询 + explicit_tools=[] → graph 不应进 tool_dispatch。"""
    # classify 故意把 needs_explicit_tools 留空（即使 user_input 看起来像 glossary，
    # 也只有 classify 说 query_class=tool + 工具名时才能触发）
    classify_resp = json.dumps(
        {
            "query_class": "definition",
            "complexity": "simple",
            "detected_language": "en",
            "rewritten_query": "AMF definition",
            "needs_explicit_tools": [],
            "reason": "definition path",
        }
    )
    generate_resp = "AMF stands for Access and Mobility Management Function [23.501 §6.3.1]."
    llm = StubLLM(responses=[classify_resp, generate_resp, _self_rag_accept_resp()])
    chunk = make_chunk("c1", spec_id="23.501", section=("6", "3", "1"))

    # 监视：如果 tool_dispatch 真的被调用，每个工具应该完全没动
    glossary_calls: list[str] = []

    async def fake_glossary(state, deps):  # type: ignore[no-untyped-def]
        glossary_calls.append(state.user_input or "")
        return {"matches": []}

    from app.tools import TOOL_REGISTRY

    monkeypatch.setitem(TOOL_REGISTRY, "glossary", fake_glossary)

    deps = make_deps(
        llm=llm,
        dense=StubDense(chunks=[chunk]),
        sparse=StubSparse(chunks=[chunk]),
        reranker=StubReranker(scores=[0.9]),
    )
    graph = build_graph(deps)

    out = await graph.ainvoke({"user_input": "What is AMF?"})
    state = AgentState.model_validate(out)
    # 主路径：classify → retrieve → rerank → generate → self_rag → END
    assert state.query_class == "definition"
    assert state.tool_results == {}  # 工具没被调用
    assert state.reranked  # 正常 retrieve / rerank 跑了
    assert "AMF" in state.final_answer
    assert glossary_calls == []  # 监视：fake_glossary 一次都没被调


# ---- 3. glossary 命中真实数据（依赖 M4.1）---------------------------------


def _bm25_available() -> bool:
    s = get_settings()
    return (Path(s.bm25_dir) / "by_spec").is_dir()


def _litellm_available() -> bool:
    return bool(get_settings().LITELLM_API_KEY.get_secret_value())


async def _pg_reachable() -> bool:
    from sqlalchemy.ext.asyncio import create_async_engine

    s = get_settings()
    try:
        eng = create_async_engine(s.DATABASE_URL, pool_pre_ping=True)
        async with eng.connect() as conn:
            from sqlalchemy import text

            await conn.execute(text("SELECT 1"))
        await eng.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def deps_for_glossary_real() -> AgentDeps:
    if not _litellm_available():
        pytest.skip("LITELLM_API_KEY 未设置")
    if not asyncio.run(_pg_reachable()):
        pytest.skip("PG 不可达")
    from app.db.base import get_sessionmaker

    s = get_settings()
    litellm = LiteLLMClient(settings=s)
    deps = AgentDeps(
        llm=litellm,
        dense=StubDense(chunks=[]),  # 工具路径用不到 retrieve
        sparse=None,
        reranker=None,
        cache=None,
        db_sessionmaker=get_sessionmaker(),
        settings=s,
    )
    yield deps
    with contextlib.suppress(RuntimeError):
        asyncio.run(deps.aclose())


@pytest.mark.asyncio
async def test_glossary_tool_hits_real_data(deps_for_glossary_real: AgentDeps) -> None:
    """glossary_tool 直接调（绕过 graph）查 AMF，验证 PG 表里能命中。

    M4.1 实测：normalized_term='AMF' 在 PG 命中 65 行。
    """
    from app.tools.glossary import glossary_tool

    state = AgentState(user_input="What is AMF?")
    out = await glossary_tool(state, deps=deps_for_glossary_real)
    assert out["warning"] is None
    matches = out["matches"]
    assert matches, "glossary 应至少命中 1 行 AMF"
    assert any(m["normalized_term"] == "amf" for m in matches)
