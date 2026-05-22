"""Web search 工具：Tavily SDK 包装，只在 `state.explicit_tools` 含 `"web_search"` 时调用。

口径见 `docs/03-development/03-agent.md §4.9 web_search`。

接口：
    async def web_search_tool(state, *, deps) -> dict[str, Any]

返回结构（写入 `state.tool_results["web_search"]`）：
    {
        "query": str,
        "results": [{"title", "url", "snippet"}, ...],
        "prefix": "以下内容来自 Web 搜索，未经 3GPP 验证：",
        "warning": str | None,
    }

实现：tavily-python AsyncClient（M0 已声明依赖）。失败 / TAVILY_API_KEY 缺失 → 空
results + warning。Tavily 同步 SDK 走 `asyncio.to_thread` 避免阻塞 event loop。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agent.deps import AgentDeps
from app.agent.state import AgentState

log = logging.getLogger(__name__)

_PREFIX = "以下内容来自 Web 搜索，未经 3GPP 验证："
_MAX_RESULTS = 5
_SNIPPET_CHARS = 480


def _build_client(api_key: str) -> Any | None:
    try:
        from tavily import TavilyClient  # type: ignore[import-untyped]
    except ImportError:
        log.warning("tavily-python not installed")
        return None
    return TavilyClient(api_key=api_key)


async def web_search_tool(state: AgentState, *, deps: AgentDeps) -> dict[str, Any]:
    query = (
        (state.rewritten_queries[0] if state.rewritten_queries else state.user_input) or ""
    ).strip()
    if not query:
        return {"query": "", "results": [], "prefix": _PREFIX, "warning": "empty query"}

    api_key = deps.settings.TAVILY_API_KEY.get_secret_value().strip()
    if not api_key:
        return {
            "query": query,
            "results": [],
            "prefix": _PREFIX,
            "warning": "TAVILY_API_KEY not set",
        }

    client = _build_client(api_key)
    if client is None:
        return {
            "query": query,
            "results": [],
            "prefix": _PREFIX,
            "warning": "tavily-python unavailable",
        }

    try:
        raw = await asyncio.to_thread(
            client.search,
            query=query,
            max_results=_MAX_RESULTS,
            search_depth="basic",
        )
    except Exception as exc:
        log.warning("web_search_tool tavily call failed: %s", exc)
        return {
            "query": query,
            "results": [],
            "prefix": _PREFIX,
            "warning": f"tavily error: {exc}",
        }

    results: list[dict[str, Any]] = []
    for item in (raw or {}).get("results") or []:
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or "")[:_SNIPPET_CHARS],
            }
        )
    _record_usage()
    return {"query": query, "results": results, "prefix": _PREFIX, "warning": None}


def _record_usage() -> None:
    """M7.4 计费 hook：tavily basic search 一次 += 1（fire-and-forget）。

    无 user 上下文（agent 在 eval / ingestion 等场景调）→ skip；任何异常 swallow。
    """
    try:
        from app.services.usage import record_web_search_usage, schedule_usage_hook

        schedule_usage_hook(record_web_search_usage(provider="tavily-search", calls=1))
    except Exception as exc:
        log.debug("usage_hook web_search failed: %s", exc)
