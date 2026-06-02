"""一次性：map-reduce 检索（A 范式）post-hoc 验证 —— multi_section (hand_crafted)
子集对 live backend 跑 + ragas，与 v11 baseline 比。

口径见 docs/04-handoff/2026-06-02-mapreduce-retrieval-plan.md §7。
- 子集 = golden v1.yaml 里 category==multi_section 且 source==hand_crafted（n=8，与 v11 对齐）
- 对 EVAL_BACKEND_BASE_URL 跑 run_eval + ragas_scorer 填 ragas_* 字段
- N_RUNS 次取统计量（judge 单 run 方差大）：per-item max-of-N（系统上限）+ mean-of-N（保守）
- 门槛（§7）：ragas_context_recall ≥ 0.72 且 ragas_faithfulness 不降（≥ 0.82）

环境变量：
- EVAL_BACKEND_BASE_URL（必填，如 https://3gpp-everything.org）
- EVAL_BACKEND_TOKEN（必填）
- N_RUNS（默认 2）

跑完即可删（不进 CI）。LiteLLM 创建于 .env，base_url 自动把 host.docker.internal→localhost。
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

import httpx
import yaml

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "eval" / "golden" / "v1.yaml"

METRICS = [
    "ragas_context_recall",
    "ragas_faithfulness",
    "ragas_answer_relevance",
    "ragas_context_precision",
    "context_recall_section",
]
# v11 baseline（docs/04-handoff/2026-05-30-ragas-4metric-uplift-results.md，multi_section n=8）
BASELINE = {
    "ragas_context_recall": 0.612,
    "ragas_faithfulness": 0.851,
    "ragas_answer_relevance": 0.775,
    "ragas_context_precision": 0.994,
}
GATE = {"ragas_context_recall": 0.72, "ragas_faithfulness": 0.82}


def build_subset() -> tuple[Path, list[str]]:
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    items = [
        it
        for it in data["items"]
        if it.get("category") == "multi_section" and it.get("source") == "hand_crafted"
    ]
    data["items"] = items
    data["total"] = len(items)
    fd, path = tempfile.mkstemp(suffix="_multisection.yaml")
    os.close(fd)
    p = Path(path)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p, [it["id"] for it in items]


def _mean(xs: list[float]) -> float | None:
    return round(statistics.mean(xs), 4) if xs else None


async def one_run(golden_path: Path, base_url: str, token: str) -> dict[str, dict]:
    from eval.ragas_eval import build_default_ragas_scorer
    from eval.runner import run_eval

    scorer = build_default_ragas_scorer()
    async with httpx.AsyncClient(base_url=base_url, timeout=180) as client:
        results = await run_eval(golden_path, client=client, auth_token=token, ragas_scorer=scorer)
    return {r.item_id: {m: getattr(r, m, None) for m in METRICS} for r in results}


def aggregate(runs: list[dict[str, dict]]) -> dict:
    item_ids = sorted({iid for run in runs for iid in run})
    out: dict[str, dict] = {}
    for m in METRICS:
        max_vals, mean_vals = [], []
        for iid in item_ids:
            vals = [
                run[iid][m]
                for run in runs
                if run.get(iid, {}).get(m) is not None
            ]
            if vals:
                max_vals.append(max(vals))
                mean_vals.append(statistics.mean(vals))
        out[m] = {"max_of_n": _mean(max_vals), "mean_of_n": _mean(mean_vals), "n_items": len(max_vals)}
    return out


async def main() -> None:
    base_url = os.environ["EVAL_BACKEND_BASE_URL"].rstrip("/")
    token = os.environ["EVAL_BACKEND_TOKEN"]
    n_runs = int(os.environ.get("N_RUNS", "2"))

    golden_path, ids = build_subset()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    outdir = REPO / "eval-results" / f"mapreduce-multisection-{ts}"
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"[mr-eval] subset n={len(ids)} ids={ids}")
    print(f"[mr-eval] base_url={base_url} n_runs={n_runs} outdir={outdir}")

    runs: list[dict[str, dict]] = []
    for i in range(n_runs):
        t0 = time.time()
        run_map = await one_run(golden_path, base_url, token)
        dt = time.time() - t0
        runs.append(run_map)
        per_run = {m: _mean([v[m] for v in run_map.values() if v.get(m) is not None]) for m in METRICS}
        print(f"[mr-eval] run {i + 1}/{n_runs} done in {dt:.0f}s  {json.dumps(per_run, ensure_ascii=False)}")
        (outdir / f"run{i + 1}.json").write_text(
            json.dumps(run_map, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    agg = aggregate(runs)
    cr = agg["ragas_context_recall"]
    fa = agg["ragas_faithfulness"]
    verdict = {
        "ctx_recall_max_of_n": cr["max_of_n"],
        "ctx_recall_mean_of_n": cr["mean_of_n"],
        "ctx_recall_gate_0.72": (cr["max_of_n"] or 0) >= GATE["ragas_context_recall"],
        "faithfulness_max_of_n": fa["max_of_n"],
        "faithfulness_mean_of_n": fa["mean_of_n"],
        "faithfulness_not_dropped_0.82": (fa["max_of_n"] or 0) >= GATE["ragas_faithfulness"],
    }
    summary = {
        "ts": ts,
        "n_runs": n_runs,
        "n_items": len(ids),
        "item_ids": ids,
        "baseline_v11": BASELINE,
        "aggregate": agg,
        "verdict": verdict,
        "pass": verdict["ctx_recall_gate_0.72"] and verdict["faithfulness_not_dropped_0.82"],
    }
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[mr-eval] ===== SUMMARY =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[mr-eval] PASS={summary['pass']}  (summary → {outdir / 'summary.json'})")


if __name__ == "__main__":
    asyncio.run(main())
