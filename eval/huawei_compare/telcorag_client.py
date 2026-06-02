"""华为 Telco-RAG 基线 connector（对比测试 B 系统采集器）。

Telco-RAG（`github.com/netop-team/Telco-RAG`，Huawei Paris Research Center）本地起服务：

    cd /data/telco-rag/Telco-RAG_api
    uvicorn api.deploy_api:app --host 0.0.0.0 --port 8000

暴露 `POST /process_query/`，body `{query, model_name, api_key}`，返回
`json.dumps({"result", "retrieval", "query"})`（注意：deploy_api 把 dict 再 `json.dumps`
一次，故 HTTP body 是「JSON 字符串的 JSON」→ 需双解析，见 `_parse_process_query_body`）。

本模块把单题答案统一成 `BaselineAnswer`，与 `eval.runner.EvalResult` 对齐（answer +
contexts），供 fact_coverage / faithfulness / pairwise judge 直接消费。

故障隔离：单题任何 HTTP / 解析异常 → `BaselineAnswer.error` 记录，不挂整批
（与 `eval.runner` / `native_mcq_runner` 同款约定）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 180.0  # Telco-RAG 单题串 rephrase→retrieve→(online)→generate，多次 OpenAI 调用
DEFAULT_CONCURRENT = 2  # 受 OpenAI rate limit + 单机 faiss 影响，保守并发
_RETRIEVAL_SPLIT = "\n\n"


@dataclass(slots=True)
class BaselineAnswer:
    """Telco-RAG 单题采集结果（系统 B）。"""

    item_id: str
    question: str
    answer: str = ""
    # Telco-RAG 返回的检索上下文（原始整段；contexts 为其切分版，供 faithfulness judge）
    retrieval_raw: str = ""
    contexts: list[str] = field(default_factory=list)
    # Telco-RAG 内部把原问题 rephrase 成的简洁 query（便于排查检索）
    rephrased_query: str = ""
    elapsed_ms: int = 0
    error: dict | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.answer.strip())

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_process_query_body(raw: object) -> dict:
    """deploy_api 返回 `json.dumps({...})` → httpx `.json()` 得到 str；再 `json.loads` 一次。

    兼容三种形态：已是 dict / JSON 字符串 / 双重转义。无法解析 → 抛 ValueError。
    """
    obj = raw
    for _ in range(2):
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, str):
            obj = json.loads(obj)
            continue
        break
    if isinstance(obj, dict):
        return obj
    raise ValueError(f"unexpected /process_query body type: {type(raw).__name__}")


def _split_contexts(retrieval_raw: str) -> list[str]:
    """把 Telco-RAG 的整段检索文本切成 context 列表（faithfulness judge 用）。

    Telco-RAG `generate()` 把上下文以段落拼接；按空行切分，去空。整段无法切 → 单元素。
    """
    if not retrieval_raw or not retrieval_raw.strip():
        return []
    parts = [p.strip() for p in retrieval_raw.split(_RETRIEVAL_SPLIT) if p.strip()]
    return parts or [retrieval_raw.strip()]


async def query_telcorag(
    item_id: str,
    question: str,
    *,
    client: httpx.AsyncClient,
    base_url: str,
    model_name: str,
    api_key: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> BaselineAnswer:
    """对单题调 Telco-RAG `/process_query/`。任何异常 → `error` 字段，不抛。"""
    url = base_url.rstrip("/") + "/process_query/"
    payload = {"query": question, "model_name": model_name, "api_key": api_key}
    t0 = time.perf_counter()
    try:
        resp = await client.post(url, json=payload, timeout=timeout_s)
        resp.raise_for_status()
        body = _parse_process_query_body(resp.json())
    except Exception as exc:  # 单题隔离：记录异常类型 + 文本，不挂整批
        return BaselineAnswer(
            item_id=item_id,
            question=question,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            error={"type": type(exc).__name__, "detail": str(exc)[:500]},
        )

    answer = str(body.get("result") or "")
    retrieval_raw = str(body.get("retrieval") or "")
    return BaselineAnswer(
        item_id=item_id,
        question=question,
        answer=answer,
        retrieval_raw=retrieval_raw,
        contexts=_split_contexts(retrieval_raw),
        rephrased_query=str(body.get("query") or ""),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        error=None if answer.strip() else {"type": "EmptyAnswer", "detail": "result 为空"},
    )


async def collect_baseline(
    items: Iterable[tuple[str, str]],
    *,
    client: httpx.AsyncClient,
    base_url: str,
    model_name: str,
    api_key: str,
    concurrent: int = DEFAULT_CONCURRENT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[BaselineAnswer]:
    """对一批 (item_id, question) 并发采集 Telco-RAG 答案，保序返回。

    单题异常隔离；并发受 `concurrent` 信号量限制。
    """
    pairs = list(items)
    sem = asyncio.Semaphore(max(1, int(concurrent)))

    async def _one(idx: int, item_id: str, question: str) -> tuple[int, BaselineAnswer]:
        async with sem:
            ans = await query_telcorag(
                item_id,
                question,
                client=client,
                base_url=base_url,
                model_name=model_name,
                api_key=api_key,
                timeout_s=timeout_s,
            )
            if ans.error:
                log.warning("telcorag item %s error: %s", item_id, ans.error)
            return idx, ans

    tasks = [asyncio.create_task(_one(i, iid, q)) for i, (iid, q) in enumerate(pairs)]
    out: list[BaselineAnswer | None] = [None] * len(pairs)
    for fut in asyncio.as_completed(tasks):
        idx, ans = await fut
        out[idx] = ans
    return [a for a in out if a is not None]


__all__ = [
    "DEFAULT_CONCURRENT",
    "DEFAULT_TIMEOUT_S",
    "BaselineAnswer",
    "collect_baseline",
    "query_telcorag",
]
