"""C = 裸 LLM 基线采集器（eval venv，**无检索**）。

对照"RAG 是否真有用，还是 LLM 靠预训练就会"（见 README §8）。给 deepseek-v4-pro 一个
3GPP 专家系统提示：正题须报出确切 TS 号、非 3GPP / 不确定就明说（给它与 RAG 同等的拒答
机会 + 让 spec 归属可比）。产出与 collect_a 同格式的 `SystemAnswer(C)` JSONL。

三系统不同源：A=mimo / B=gpt-4o-mini / C=deepseek-v4-pro → 裁判用 glm-5.1。

用法（eval venv）：
    PYTHONPATH=/data/3GPP-Everything eval/.venv/bin/python -m eval.huawei_compare.collect_c \
        --in eval-results/huawei-compare/questions.jsonl \
        --out eval-results/huawei-compare/c_answers.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path

import httpx

from eval.huawei_compare.build_intersection import normalize_spec_id
from eval.huawei_compare.schema import SystemAnswer, dump_jsonl, load_questions
from eval.settings import EvalSettings, get_settings
from eval.teleqna.infer import DEFAULT_TIMEOUT_S, _LiteLLMChatClient

SYSTEM_C = "C"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_MAX_TOKENS = 8192  # deepseek-v4-pro 是 reasoning 模型,reasoning 吃 token,给足避免空答
DEFAULT_CONCURRENT = 6
DEFAULT_RPM = 50
# reasoning 长度非确定,偶发吃光预算吐空 content → 同参重试(下次 reasoning 可能更短)兜底
EMPTY_RETRIES = 3

_SYSTEM_PROMPT = (
    "You are a 3GPP telecommunications standards expert. Answer the user's question "
    "from your own knowledge — you have NO retrieval/document access.\n"
    "- If the question is about a topic that 3GPP specifies, give your best answer AND "
    "identify the exact 3GPP TS spec number on its own line in the form 'SPEC: TS xx.xxx' "
    "(add the clause if you can).\n"
    "- If the topic is NOT specified by 3GPP (e.g. it belongs to IETF/IEEE/ITU), or you "
    "are not sure the concept exists, say so clearly instead of guessing."
)

# 从答案里抠出 C 自报的 spec 号（"SPEC: TS 23.501 ..." / 文中 "TS 38.331"）
_SPEC_LINE_RE = re.compile(r"SPEC:\s*(.+)", re.IGNORECASE)
_SPEC_NUM_RE = re.compile(r"\b\d{2}\.\d{3}(?:-\d{1,3})?\b")


def parse_c_cited_specs(answer: str) -> list[str]:
    """C 自报的 spec：优先 'SPEC:' 行，回退全文首个 spec 号。去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    m = _SPEC_LINE_RE.search(answer or "")
    search_space = m.group(1) if m else (answer or "")
    for num in _SPEC_NUM_RE.findall(search_space):
        sid = normalize_spec_id(num)
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


async def _collect_one(
    q: dict,
    *,
    client: _LiteLLMChatClient,
    sem: asyncio.Semaphore,
    max_tokens: int,
) -> SystemAnswer:
    iid, question = q["item_id"], q["question"]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    answer = ""
    async with sem:
        t0 = time.perf_counter()
        for _ in range(EMPTY_RETRIES + 1):
            try:
                payload = await client.chat(
                    messages=messages, max_tokens=max_tokens, temperature=0.0
                )
            except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as exc:
                return SystemAnswer(
                    item_id=iid,
                    question=question,
                    system=SYSTEM_C,
                    elapsed_ms=int((time.perf_counter() - t0) * 1000),
                    error={"type": type(exc).__name__, "detail": str(exc)[:500]},
                    meta={"model": client.model},
                )
            answer = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if answer.strip():  # 非空即收;空(reasoning 溢出)则重试
                break
    return SystemAnswer(
        item_id=iid,
        question=question,
        system=SYSTEM_C,
        answer=answer,
        contexts=[],  # 裸 LLM 无检索上下文
        cited_specs=parse_c_cited_specs(answer),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        error=None if answer.strip() else {"type": "EmptyAnswer"},
        meta={"model": client.model},
    )


async def collect_c(
    questions: list[dict],
    *,
    client: _LiteLLMChatClient,
    concurrent: int = DEFAULT_CONCURRENT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[SystemAnswer]:
    sem = asyncio.Semaphore(concurrent)
    tasks = [
        asyncio.create_task(_collect_one(q, client=client, sem=sem, max_tokens=max_tokens))
        for q in questions
    ]
    out: list[SystemAnswer] = []
    for i, fut in enumerate(asyncio.as_completed(tasks), 1):
        out.append(await fut)
        if i % 20 == 0:
            print(f"C [{i}/{len(questions)}]", flush=True)
    # 保持输入顺序
    by_id = {a.item_id: a for a in out}
    return [by_id[q["item_id"]] for q in questions if q["item_id"] in by_id]


def build_client(
    settings: EvalSettings | None = None, model: str = DEFAULT_MODEL
) -> _LiteLLMChatClient:
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise RuntimeError("LITELLM_API_KEY missing in env/.env")
    return _LiteLLMChatClient(
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        model=model,
        timeout_s=DEFAULT_TIMEOUT_S,
    )


async def _run(args: argparse.Namespace) -> int:
    questions = load_questions(Path(args.infile))
    client = build_client(model=args.model)
    try:
        answers = await collect_c(questions, client=client, concurrent=args.concurrent)
    finally:
        await client.aclose()
    n = dump_jsonl((a.to_dict() for a in answers), Path(args.outfile))
    ok = sum(1 for a in answers if a.ok)
    print(f"C 采集完成：{n} 题写入 {args.outfile}（ok={ok}/{n}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", dest="outfile", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrent", type=int, default=DEFAULT_CONCURRENT)
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
