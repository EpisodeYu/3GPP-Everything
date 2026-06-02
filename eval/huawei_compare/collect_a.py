"""A = 3GPP-Everything 采集器（eval venv）。

走 `eval.runner.call_agent`（HTTP /chat SSE）→ `SystemAnswer(A)` → a_answers.jsonl。

用法（eval venv）：
    EVAL_BACKEND_BASE_URL=http://<tgpp-net 容器IP>:8002 \
    EVAL_BACKEND_TOKEN=$(cat /tmp/tgpp-eval-token.txt) \
    uv run --project eval python -m eval.huawei_compare.collect_a \
        --in eval/huawei_compare/smoke_questions.jsonl \
        --out eval-results/huawei-compare-smoke/a_answers.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path

import httpx

from eval.huawei_compare.schema import SYSTEM_A, SystemAnswer, dump_jsonl, load_questions
from eval.runner import AgentResponse, call_agent

log = logging.getLogger(__name__)


def _a_contexts(resp: AgentResponse, *, limit: int = 12) -> list[str]:
    """A 的检索上下文：取 chunks_rerank（fallback chunks_hit）每条的完整文本。"""
    chunks = list(resp.chunks_rerank or resp.chunks_hit or [])
    out: list[str] = []
    for c in chunks[:limit]:
        text = str(c.get("content") or c.get("preview") or c.get("text") or "").strip()
        if text:
            out.append(text)
    return out


def _a_cited_specs(resp: AgentResponse) -> list[str]:
    """A 引用 spec_id：优先 citations，fallback chunks_rerank；去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for src in (resp.citations, resp.chunks_rerank, resp.chunks_hit):
        for c in src or []:
            sid = str(c.get("spec_id") or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
        if out:
            break
    return out


def _to_answer(item_id: str, question: str, resp: AgentResponse) -> SystemAnswer:
    error = None
    if resp.terminal_event != "final":
        error = resp.error or {"type": "terminal", "detail": resp.terminal_event}
    return SystemAnswer(
        item_id=item_id,
        question=question,
        system=SYSTEM_A,
        answer=resp.answer or "",
        contexts=_a_contexts(resp),
        cited_specs=_a_cited_specs(resp),
        elapsed_ms=resp.duration_ms,
        error=error,
        meta={"terminal_event": resp.terminal_event, "confidence": resp.confidence},
    )


async def collect_a(
    questions: list[dict],
    *,
    base_url: str,
    token: str,
    timeout_s: float = 180.0,
) -> list[SystemAnswer]:
    """顺序采集（每题独立 session，避免历史污染检索）。单题异常隔离。"""
    out: list[SystemAnswer] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
        for i, q in enumerate(questions, start=1):
            iid, question = q["item_id"], q["question"]
            t0 = time.perf_counter()
            try:
                resp = await call_agent(client=client, auth_token=token, question=question)
                ans = _to_answer(iid, question, resp)
            except Exception as exc:  # 单题隔离
                ans = SystemAnswer(
                    item_id=iid,
                    question=question,
                    system=SYSTEM_A,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                    error={"type": type(exc).__name__, "detail": str(exc)[:500]},
                )
            log.info("A [%d/%d] %s ok=%s", i, len(questions), iid, ans.ok)
            out.append(ans)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True, type=Path)
    ap.add_argument("--out", dest="outfile", required=True, type=Path)
    ap.add_argument("--base-url", default=os.environ.get("EVAL_BACKEND_BASE_URL", ""))
    ap.add_argument("--token", default=os.environ.get("EVAL_BACKEND_TOKEN", ""))
    args = ap.parse_args()
    if not args.base_url or not args.token:
        ap.error("需要 --base-url/--token 或 EVAL_BACKEND_BASE_URL/EVAL_BACKEND_TOKEN 环境变量")

    questions = load_questions(args.infile)
    answers = asyncio.run(collect_a(questions, base_url=args.base_url, token=args.token))
    n = dump_jsonl((a.to_dict() for a in answers), args.outfile)
    ok = sum(1 for a in answers if a.ok)
    print(f"A 采集完成：{n} 题写入 {args.outfile}（ok={ok}/{n}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
