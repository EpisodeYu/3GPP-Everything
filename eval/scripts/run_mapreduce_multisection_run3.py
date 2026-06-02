"""map-reduce multi_section 严谨补跑：run3（长 ragas 超时 + 串行救超时）+ max-of-3。

承接 run_mapreduce_multisection.py 的 run1/run2（默认 timeout=180s，6 次 TimeoutError →
run2 faithfulness 4 题 null）。本脚本：
- 跑第 3 轮：逐题 call_agent + scorer.score_item(run_config=RunConfig(timeout=600, max_workers=2))，
  把长答案 faithfulness 超时救回来。
- 与已存的 run1.json / run2.json 合并算 max-of-3 / mean-of-3（与 v11 baseline 同口径）。
- 写 summary_max3.json + 打印 PASS/FAIL（ctx_recall ≥ 0.72 且 faithfulness ≥ 0.82）。

环境变量：EVAL_BACKEND_BASE_URL / EVAL_BACKEND_TOKEN。
口径见 docs/04-handoff/2026-06-02-mapreduce-retrieval-plan.md §7。跑完可删。
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics as st
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "eval" / "golden" / "v1.yaml"
RUNDIR = REPO / "eval-results" / "mapreduce-multisection-20260602T054430Z"

METRICS = [
    "ragas_context_recall",
    "ragas_faithfulness",
    "ragas_answer_relevance",
    "ragas_context_precision",
    "context_recall_section",
]
BASELINE = {
    "ragas_context_recall": 0.612,
    "ragas_faithfulness": 0.851,
    "ragas_answer_relevance": 0.775,
    "ragas_context_precision": 0.994,
}
GATE = {"ragas_context_recall": 0.72, "ragas_faithfulness": 0.82}


def _mean(xs: list[float]) -> float | None:
    return round(st.mean(xs), 4) if xs else None


async def run3() -> dict[str, dict]:
    from ragas.run_config import RunConfig

    from eval.ragas_eval import build_default_ragas_scorer
    from eval.runner import call_agent, compute_eval_metrics
    from eval.runner_retrieval import load_golden

    base = os.environ["EVAL_BACKEND_BASE_URL"].rstrip("/")
    token = os.environ["EVAL_BACKEND_TOKEN"]
    items = [
        it
        for it in load_golden(GOLDEN)
        if it.category == "multi_section" and it.source == "hand_crafted"
    ]
    scorer = build_default_ragas_scorer()
    rc = RunConfig(timeout=600, max_workers=2)  # 长超时 + 适度并发救超时
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(base_url=base, timeout=240) as client:
        for it in items:
            t0 = time.time()
            resp = await call_agent(client=client, auth_token=token, question=it.question, mode="qa")
            result = compute_eval_metrics(it, resp)
            scores: dict = {}
            if resp.answer:
                try:
                    scores = scorer.score_item(it, resp, run_config=rc)
                except Exception as exc:  # noqa: BLE001
                    print(f"[run3] {it.id} ragas crashed: {exc}")
            out[it.id] = {
                "ragas_context_recall": scores.get("ragas_context_recall"),
                "ragas_faithfulness": scores.get("ragas_faithfulness"),
                "ragas_answer_relevance": scores.get("ragas_answer_relevance"),
                "ragas_context_precision": scores.get("ragas_context_precision"),
                "context_recall_section": result.context_recall_section,
            }
            print(f"[run3] {it.id} {time.time() - t0:.0f}s {json.dumps(out[it.id], ensure_ascii=False)}")
    return out


def aggregate(runs: list[dict[str, dict]]) -> dict:
    item_ids = sorted({iid for run in runs for iid in run})
    agg: dict[str, dict] = {}
    for m in METRICS:
        max_vals, mean_vals = [], []
        for iid in item_ids:
            vals = [run[iid][m] for run in runs if run.get(iid, {}).get(m) is not None]
            if vals:
                max_vals.append(max(vals))
                mean_vals.append(st.mean(vals))
        agg[m] = {"max_of_n": _mean(max_vals), "mean_of_n": _mean(mean_vals), "n_items": len(max_vals)}
    return agg


async def main() -> None:
    r3 = await run3()
    (RUNDIR / "run3.json").write_text(json.dumps(r3, ensure_ascii=False, indent=2), encoding="utf-8")

    runs = [json.loads((RUNDIR / f"run{i}.json").read_text(encoding="utf-8")) for i in (1, 2)]
    runs.append(r3)
    agg = aggregate(runs)
    cr, fa = agg["ragas_context_recall"], agg["ragas_faithfulness"]
    summary = {
        "method": "max-of-3 (run1/run2 @180s + run3 @600s serial-ish)",
        "n_runs": 3,
        "n_items": len(r3),
        "baseline_v11": BASELINE,
        "aggregate": agg,
        "verdict": {
            "ctx_recall_max_of_3": cr["max_of_n"],
            "ctx_recall_mean_of_3": cr["mean_of_n"],
            "ctx_recall_gate_0.72": (cr["max_of_n"] or 0) >= GATE["ragas_context_recall"],
            "faithfulness_max_of_3": fa["max_of_n"],
            "faithfulness_mean_of_3": fa["mean_of_n"],
            "faithfulness_gate_0.82": (fa["max_of_n"] or 0) >= GATE["ragas_faithfulness"],
        },
        "pass": (cr["max_of_n"] or 0) >= GATE["ragas_context_recall"]
        and (fa["max_of_n"] or 0) >= GATE["ragas_faithfulness"],
    }
    (RUNDIR / "summary_max3.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[run3] ===== SUMMARY (max-of-3) =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
