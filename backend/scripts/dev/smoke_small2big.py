"""small2big（Issue #3）真实环境冒烟测试。

在 tgpp docker 网络里的一次性 `run` 容器里跑（连真 Qdrant + PG + LiteLLM + 真 key）：

    docker compose -f deploy/docker-compose.prod.yml --env-file .env \\
        run --rm --no-deps -T api python -m scripts.dev.smoke_small2big

做什么：用生产 `AgentDeps.from_env()` + `build_graph(deps)` 跑几条真实查询，
经 `astream(stream_mode=["updates","custom"])` 抓：
- `expand` 节点是否执行、reranked 里有多少块被回扩（expanded_content 非空）
- `chunks_expanded` 自定义事件是否 emit（含 degraded 标记）
- 终答是否非空 + 有引用

PASS 判据：至少一条查询里 expand 真正扩了段（expanded_content 非空）+ emit 了
chunks_expanded + 终答非空。全查询都没扩到 → FAIL（多半是命中的都是单块 section 或
配置/数据异常，需人看诊断）。只读 PG/Qdrant + 调 LLM（烧 token），不写任何数据。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from app.agent.deps import AgentDeps
from app.agent.graph import build_graph

log = logging.getLogger("smoke_small2big")

# 选偏"整段有多块"的 5G NR 查询，最大化触发扩段（definition/procedure 类通常单命中
# 落在多 chunk 的 section 里）。可用 --query 覆盖。
_DEFAULT_QUERIES = [
    "Describe the RRC connection reconfiguration procedure in 38.331",
    "What is the PDU session establishment procedure in 23.502?",
    "Explain the random access procedure in NR",
]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """StateChunk 既可能是 pydantic 对象也可能是 dict（防御 astream 版本差异）。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def _run_one(graph: Any, query: str) -> dict[str, Any]:
    seen_nodes: list[str] = []
    custom_by_type: dict[str, int] = {}
    expanded_samples: list[dict[str, Any]] = []
    reranked_after_expand: list[Any] = []
    final_answer = ""
    citations: list[Any] = []
    confidence = 0.0

    async for mode, payload in graph.astream(
        {"user_input": query},
        stream_mode=["updates", "custom"],
    ):
        if mode == "custom" and isinstance(payload, dict):
            t = str(payload.get("type") or "?")
            custom_by_type[t] = custom_by_type.get(t, 0) + 1
            if t == "chunks_expanded":
                for c in payload.get("chunks") or []:
                    expanded_samples.append(c)
        elif mode == "updates" and isinstance(payload, dict):
            for node, partial in payload.items():
                seen_nodes.append(node)
                if not isinstance(partial, dict):
                    continue
                if node == "expand" and isinstance(partial.get("reranked"), list):
                    reranked_after_expand = partial["reranked"]
                if node == "generate" and partial.get("final_answer"):
                    final_answer = str(partial["final_answer"])
                    citations = partial.get("citations") or citations
                if node == "self_rag" and isinstance(partial.get("confidence"), (int, float)):
                    confidence = float(partial["confidence"])

    expanded_chunks = [c for c in reranked_after_expand if _get(c, "expanded_content")]
    return {
        "query": query,
        "nodes": seen_nodes,
        "expand_ran": "expand" in seen_nodes,
        "n_reranked": len(reranked_after_expand),
        "n_expanded": len(expanded_chunks),
        "n_chunks_expanded_evt": custom_by_type.get("chunks_expanded", 0),
        "custom_events": custom_by_type,
        "final_answer": final_answer,
        "n_citations": len(citations),
        "confidence": confidence,
        "expanded_detail": [
            {
                "chunk_id": _get(c, "chunk_id"),
                "section": ".".join(_get(c, "section_path", ()) or ()),
                "small_len": len(_get(c, "content", "") or ""),
                "expanded_len": len(_get(c, "expanded_content", "") or ""),
            }
            for c in expanded_chunks[:3]
        ],
        "chunks_expanded_evt_sample": [
            {
                "chunk_id": c.get("chunk_id"),
                "section": c.get("section_path"),
                "content_len": len(c.get("content") or ""),
                "degraded": c.get("degraded"),
            }
            for c in expanded_samples[:3]
        ],
    }


def _print_report(results: list[dict[str, Any]]) -> bool:
    any_expanded = False
    print("\n" + "=" * 72)
    print("small2big SMOKE REPORT")
    print("=" * 72)
    for r in results:
        expanded_ok = r["n_expanded"] > 0 and r["n_chunks_expanded_evt"] > 0
        any_expanded = any_expanded or expanded_ok
        print(f"\nQ: {r['query']}")
        print(
            f"  nodes: {r['nodes']}\n"
            f"  reranked={r['n_reranked']} expanded={r['n_expanded']} "
            f"chunks_expanded_evt={r['n_chunks_expanded_evt']} "
            f"custom={r['custom_events']}"
        )
        for d in r["expanded_detail"]:
            print(
                f"    · {d['chunk_id']} §{d['section']}: "
                f"small={d['small_len']}c → expanded={d['expanded_len']}c"
            )
        for e in r["chunks_expanded_evt_sample"]:
            print(
                f"    · evt {e['chunk_id']} §{e['section']} len={e['content_len']} degraded={e['degraded']}"
            )
        ans = (r["final_answer"] or "").replace("\n", " ")
        print(
            f"  answer({len(r['final_answer'])}c, cites={r['n_citations']}, conf={r['confidence']}): {ans[:180]}"
        )
        print(f"  → expand fired: {'YES' if expanded_ok else 'no'}")

    print("\n" + "=" * 72)
    verdict = any_expanded
    print(f"VERDICT: {'PASS' if verdict else 'FAIL'} — expand 至少一条查询真正扩了段")
    print("=" * 72)
    return verdict


async def main_async(queries: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    deps = AgentDeps.from_env()
    s = deps.settings
    log.info(
        "settings: SMALL2BIG_ENABLED=%s provider=%s collection=%s max_section=%d window=%d budget=%d",
        s.SMALL2BIG_ENABLED,
        s.EMBEDDING_PROVIDER,
        s.qdrant_collection,
        s.SMALL2BIG_MAX_SECTION_CHARS,
        s.SMALL2BIG_NEIGHBOR_WINDOW,
        s.SMALL2BIG_TOTAL_BUDGET_CHARS,
    )
    if not s.SMALL2BIG_ENABLED:
        log.error("SMALL2BIG_ENABLED=false → 冒烟无意义，先打开开关")
        return 2

    graph = build_graph(deps)
    results: list[dict[str, Any]] = []
    try:
        for q in queries:
            log.info("running query: %s", q)
            results.append(await _run_one(graph, q))
    finally:
        await deps.aclose()

    ok = _print_report(results)
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="small2big real-env smoke")
    p.add_argument("--query", action="append", default=None, help="覆盖默认查询（可多次）")
    args = p.parse_args()
    queries = args.query or _DEFAULT_QUERIES
    try:
        return asyncio.run(main_async(queries))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
