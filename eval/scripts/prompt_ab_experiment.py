"""Prompt A/B 实验：固定检索上下文，比 5 个 generate prompt 思路的 ragas 表现。

方法（隔离纯 prompt 效应）：
1. 对 10 道 hand_crafted 题，用 call_agent 打 live backend 抓 reranked 上下文（含正文）
   —— 这是 generate 节点实际看到的 context，全程固定。
2. 同一 context、同一生成模型（LLM_AGENT_MODEL=mimo-v2.5-pro）、同温度 0.1，离线跑
   5 个 prompt：v4(当前部署) + P1 抽取式 / P2 结构化 / P3 紧扣所问 / P4 草稿自检。
3. 每个答案用 ragas（faithfulness/answer_relevance）+ 子串 fact_coverage + 长度打分。
4. 按 prompt 聚合 10 题，看谁更好。

用法：
    PYTHONPATH=/data/3GPP-Everything EVAL_BACKEND_IP=172.22.0.5 \\
      uv run --project eval python eval/scripts/prompt_ab_experiment.py
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics as st
from pathlib import Path

import httpx
from eval.ragas_eval import build_default_ragas_scorer
from eval.runner import AgentResponse, _fact_coverage, call_agent
from eval.runner_retrieval import load_golden
from eval.settings import EvalSettings

QIDS = [
    "hand-def-002", "hand-def-006", "hand-def-007",
    "hand-proc-001", "hand-proc-004",
    "hand-formula-001", "hand-formula-003",
    "hand-multi-004", "hand-table-002", "hand-table-005",
]
OUT = Path("eval-results/2026-05-28-prompt-ab")
GEN_MODEL = os.getenv("LLM_AGENT_MODEL", "mimo-v2.5-pro")

_HEADER = """You are a senior 3GPP standards engineer answering STRICTLY on the basis of the
retrieved chunks below. Behave as a careful technical writer.

Hard rules — violations are unacceptable:
1. NEVER fabricate. If the chunks do not support the answer, say so in the user's
   language and stop.
2. EVERY normative claim ends with `[spec_id §section_path]`, e.g. `[38.331 §5.3.5]`;
   copy spec_id/section_path verbatim from the chunk metadata line; NO space after §.
3. Use chunk wording verbatim for defined terms / IE / message names.
4. Preserve LaTeX math as `$...$`.
5. Output language: {lang} (zh = Simplified Chinese, keep technical names in English).
"""

_APPROACH = {
    "v4_current": """6. Answer the QUESTION asked, completely — cover the relevant normative points the
   chunks support; don't cut a needed procedure/condition set short. BUT:
   - Every statement MUST be directly supported by a cited chunk; add nothing beyond
     what the cited chunks state.
   - Do NOT dump tangential sub-cases or release-specific edge cases the question did
     not ask. Prefer the core definition/procedure over listing every clause.
   - No padding/repetition/filler.""",
    "P1_extractive": """6. APPROACH — Extractive grounding:
   - Answer using ONLY information explicitly stated in the chunks. Prefer quoting or
     closely paraphrasing the chunk wording; do not synthesize, generalize, or add
     background beyond the chunks.
   - Every sentence must be directly traceable to a specific chunk. If something is
     not in the chunks, do not say it.
   - Lead with the definition/answer exactly as the chunk states it, then only the
     directly-stated supporting details.""",
    "P2_structured": """6. APPROACH — Fixed structure, fill then STOP. Use EXACTLY these sections, nothing more:
   **核心定义/直接答案**：1-2 句直接回答问题。
   **关键要素**：chunks 明确给出的关键字段/参数/步骤，逐条引用。
   **适用范围/条件**：仅与问题直接相关的条件（无则省略本节）。
   Fill from the chunks, then STOP. Do NOT add extra sections, tangential edge cases,
   or release-specific enumerations.""",
    "P3_scope_tight": """6. APPROACH — Answer exactly what is asked:
   - First, in ONE short line, restate what the question asks for.
   - Then answer EXACTLY that and nothing the question did not ask. If asked "what is
     X", define X and its directly-relevant attributes, then stop.
   - Actively resist listing tangential sub-cases, conditions, or release-specific
     details not needed to answer the question.""",
    "P4_self_verify": """6. APPROACH — Draft then verify (output only the final answer):
   - Internally draft a complete answer, then re-check each sentence against the
     chunks and DROP any sentence not directly supported by a cited chunk.
   - Be complete on what the question asks, but every sentence in the FINAL output
     must be grounded in a cited chunk.
   - Output ONLY the verified final answer (do not show drafting/checking).""",
}

_STRUCT = """
Output structure: concise direct answer first (1-3 sentences), then bullets/short
paragraphs with `[spec §...]` citations.
"""


def build_prompt(approach_key: str, chunks: list[dict], question: str, lang: str) -> str:
    parts = [_HEADER.format(lang=lang), _APPROACH[approach_key], _STRUCT]
    parts.append(f"\nRetrieved chunks (top {len(chunks)}):")
    for i, c in enumerate(chunks, 1):
        sec = c.get("section_path") or ""
        if isinstance(sec, list):
            sec = ".".join(str(x) for x in sec)
        parts.append(f"---\n[{i}] spec_id={c.get('spec_id')} section_path={sec} title={c.get('section_title')}")
        parts.append(c.get("content") or "")
    parts.append(f"---\n\nUser question ({lang}):\n{question}")
    return "\n".join(parts)


async def generate(client: httpx.AsyncClient, key: str, prompt: str) -> str:
    """生成；超时/网络错重试 2 次，仍失败返回空串（调用方记 None，不中断整轮）。"""
    last = None
    for attempt in range(3):
        try:
            r = await client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": GEN_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
            )
            r.raise_for_status()
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"      generate 重试 {attempt+1}/3: {type(exc).__name__}", flush=True)
            await asyncio.sleep(3)
    print(f"      generate 放弃: {last}", flush=True)
    return ""


async def main() -> None:
    s = EvalSettings()
    ip = os.environ["EVAL_BACKEND_IP"]
    token = Path("/tmp/tgpp-eval-token.txt").read_text().strip()
    golden = {it.id: it for it in load_golden(Path("eval/golden/v1.yaml"))}

    from ragas.run_config import RunConfig

    scorer = build_default_ragas_scorer(s)
    rc = RunConfig(timeout=900, max_workers=2)

    OUT.mkdir(parents=True, exist_ok=True)
    backend = httpx.AsyncClient(base_url=f"http://{ip}:8002", timeout=200)
    llm = httpx.AsyncClient(base_url=s.resolved_litellm_base_url, timeout=150)

    rows_path = OUT / "rows.json"
    rows = json.loads(rows_path.read_text()) if rows_path.exists() else []
    done = {(r["qid"], r["prompt"]) for r in rows}  # 断点续跑

    for qi, qid in enumerate(QIDS, 1):
        item = golden[qid]
        if all((qid, k) in done for k in _APPROACH):
            print(f"\n[{qi}/{len(QIDS)}] {qid} — 已完成，跳过", flush=True)
            continue
        print(f"\n[{qi}/{len(QIDS)}] {qid} — 抓上下文…", flush=True)
        try:
            resp = await call_agent(client=backend, auth_token=token, question=item.question)
            ctx = resp.chunks_rerank or []
        except Exception as exc:  # noqa: BLE001
            print(f"   call_agent 失败，跳过该题: {exc}", flush=True)
            continue
        print(f"   ctx chunks={len(ctx)} (content有={sum(1 for c in ctx if c.get('content'))})", flush=True)
        for key in _APPROACH:
            if (qid, key) in done:
                continue
            prompt = build_prompt(key, ctx, item.question, item.language or "zh")
            ans = await generate(llm, s.litellm_api_key, prompt)
            sc = {}
            if ans:
                try:
                    scored = AgentResponse(answer=ans, chunks_rerank=ctx, terminal_event="final")
                    sc = scorer.score_item(item, scored, run_config=rc)
                except Exception as exc:  # noqa: BLE001
                    print(f"      score 失败: {exc}", flush=True)
            fc = _fact_coverage(ans, item.expected_facts) if ans else None
            rows.append({
                "qid": qid, "category": item.category, "prompt": key,
                "ans_len": len(ans),
                "faith": sc.get("ragas_faithfulness"),
                "ans_rel": sc.get("ragas_answer_relevance"),
                "fact_cov": fc, "answer": ans,
            })
            rows_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))  # 增量落盘
            print(f"   {key:14} len={len(ans):5} faith={sc.get('ragas_faithfulness')} relev={sc.get('ragas_answer_relevance')} fact={fc}", flush=True)

    # 聚合
    print("\n\n========== 按 prompt 聚合（10 题均值） ==========")
    print(f"{'prompt':14}{'n_faith':>8}{'faith':>8}{'ans_rel':>9}{'fact_cov':>9}{'avg_len':>9}")
    def m(xs): xs=[x for x in xs if isinstance(x,(int,float))]; return round(st.mean(xs),3) if xs else None
    for key in _APPROACH:
        pr=[r for r in rows if r["prompt"]==key]
        fa=[r["faith"] for r in pr if r["faith"] is not None]
        print(f"{key:14}{len(fa):>8}{str(m([r['faith'] for r in pr])):>8}{str(m([r['ans_rel'] for r in pr])):>9}{str(m([r['fact_cov'] for r in pr])):>9}{str(m([r['ans_len'] for r in pr])):>9}")
    await backend.aclose(); await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
