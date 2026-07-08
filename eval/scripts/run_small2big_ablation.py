"""一次性：small2big 扩段（Issue #3）扩/不扩 ablation —— 对 live backend 跑 + ragas。

口径见 issue #3 验收 + docs/03-development/03-agent.md §4.6b。

**关键：ablation 的开关在 backend 侧**（`SMALL2BIG_ENABLED` env），eval 只是打分客户端，
无法从 client 翻转 backend 配置。所以流程是「跑两趟、人翻转 backend」：

  1. backend 配 `SMALL2BIG_ENABLED=false` 重启 → `SMALL2BIG_MODE=off  python -m eval.scripts.run_small2big_ablation`
  2. backend 配 `SMALL2BIG_ENABLED=true`  重启 → `SMALL2BIG_MODE=on ABLATION_COMPARE_TO=<off 的 summary.json> python -m eval.scripts.run_small2big_ablation`

第二趟带 `ABLATION_COMPARE_TO` 时会打印 on−off 的 delta。

指标口径（为何是这几个）：
- `ragas_context_recall`（内容级）：small2big 把整段 section 喂 LLM，`_join_contexts`
  按 chunk_id 用 `chunks_expanded` 覆盖小块 → 真正反映 LLM 看到的上下文，这是主指标。
- `fact_coverage`：答案级覆盖度（LLM 看到更全 → 答得更全），主指标。
- `ragas_faithfulness`：护栏，不能因扩段引入噪声而降。
- `duration_ms` P50/P95：护栏，扩段多一次 PG + Qdrant 往返 + 更长 prompt，关注延迟回归。
- 注：二值 `context_recall_section`（section-hit）按构造不动（扩段不新增 section），
  不作为 ablation 主指标。

环境变量：
- EVAL_BACKEND_BASE_URL（必填，如 https://3gpp-everything.org）
- EVAL_BACKEND_TOKEN（必填）
- N_RUNS（默认 2；judge 单 run 方差大，取统计量）
- SMALL2BIG_MODE（"on" / "off"，仅作输出标签；需与 backend 实际配置一致）
- ABLATION_CATEGORIES（逗号分隔，过滤 golden category；默认全部 hand_crafted item）
- ABLATION_COMPARE_TO（可选：另一趟 summary.json 路径，打印 delta）

跑完即可删（不进 CI）。需要 live backend + 真 ragas key（烧 token），归人执行 / 审批。
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "eval" / "golden" / "v1.yaml"

# 质量指标（取 max/mean-of-n）；duration_ms 单独算 P50/P95。
# negative_pass：VALID_REFUSAL=1 / PARTIAL=0.5 / INVALID=0（仅 negative item 有值）。
QUALITY_METRICS = [
    "ragas_context_recall",
    "fact_coverage",
    "ragas_faithfulness",
    "ragas_answer_relevance",
    "ragas_context_precision",
    "context_recall_section",
    "context_recall_spec",
    "negative_pass",
]

_NEG_VERDICT_NUM = {"VALID_REFUSAL": 1.0, "PARTIAL_REFUSAL": 0.5, "INVALID": 0.0}


def build_subset() -> tuple[Path, list[str]]:
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    cats_env = os.environ.get("ABLATION_CATEGORIES", "").strip()
    cats = {c.strip() for c in cats_env.split(",") if c.strip()} if cats_env else None
    items = [
        it
        for it in data["items"]
        if it.get("source") == "hand_crafted" and (cats is None or it.get("category") in cats)
    ]
    data["items"] = items
    data["total"] = len(items)
    fd, path = tempfile.mkstemp(suffix="_small2big.yaml")
    os.close(fd)
    p = Path(path)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p, [it["id"] for it in items]


def _mean(xs: list[float]) -> float | None:
    return round(statistics.mean(xs), 4) if xs else None


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    # nearest-rank：小样本稳，不插值
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[idx], 1)


class _TimeoutScorer:
    """包一层 ragas scorer，给每次 score_item 注入更长 timeout 的 RunConfig。

    run_eval 调 `scorer.score_item(it, resp)`（不传 run_config），默认走 ragas 内置
    timeout=180s，长答案 faithfulness 常 TimeoutError。这里用 RAGAS_TIMEOUT_S 抬高
    per-job timeout 降低超时率（超时项仍返回 None，聚合自动跳过 = "不计入对比"）。
    """

    def __init__(self, inner: Any, timeout_s: float, max_workers: int) -> None:
        from ragas.run_config import RunConfig

        self._inner = inner
        self._rc = RunConfig(timeout=int(timeout_s), max_workers=max_workers)

    def score_item(self, item: Any, resp: Any) -> dict[str, float | None]:
        return self._inner.score_item(item, resp, run_config=self._rc)


def _build_judges() -> tuple[Any, Any, Any]:
    """ragas scorer + negative judge + fact_coverage judge；任一缺 key/包 → None（降级）。"""
    from eval.ragas_eval import build_default_ragas_scorer

    scorer: Any = build_default_ragas_scorer()  # 缺 key 直接抛，ablation 无意义故不吞
    timeout_s = float(os.environ.get("RAGAS_TIMEOUT_S", "300"))
    max_workers = int(os.environ.get("RAGAS_MAX_WORKERS", "16"))
    scorer = _TimeoutScorer(scorer, timeout_s, max_workers)
    negative_judge: Any = None
    fact_judge: Any = None
    try:
        from eval.negative_judge import build_default_negative_judge

        negative_judge = build_default_negative_judge()
    except Exception as e:
        print(f"[s2b-eval] negative_judge disabled: {e}")
    try:
        from eval.fact_coverage_judge import build_default_fact_coverage_judge

        fact_judge = build_default_fact_coverage_judge()
    except Exception as e:
        print(f"[s2b-eval] fact_coverage_judge disabled (fallback substring): {e}")
    return scorer, negative_judge, fact_judge


async def one_run(golden_path: Path, base_url: str, token: str) -> dict[str, dict]:
    from eval.runner import run_eval

    scorer, negative_judge, fact_judge = _build_judges()
    async with httpx.AsyncClient(base_url=base_url, timeout=180) as client:
        results = await run_eval(
            golden_path,
            client=client,
            auth_token=token,
            ragas_scorer=scorer,
            negative_judge=negative_judge,
            fact_coverage_judge=fact_judge,
        )
    out: dict[str, dict] = {}
    for r in results:
        row: dict[str, Any] = {
            m: getattr(r, m, None) for m in QUALITY_METRICS if m != "negative_pass"
        }
        row["negative_pass"] = _NEG_VERDICT_NUM.get(
            getattr(r, "negative_judge_verdict", None) or ""
        )
        row["duration_ms"] = getattr(r, "duration_ms", 0)
        out[r.item_id] = row
    return out


def aggregate(runs: list[dict[str, dict]]) -> dict:
    item_ids = sorted({iid for run in runs for iid in run})
    out: dict[str, dict] = {}
    for m in QUALITY_METRICS:
        max_vals, mean_vals = [], []
        for iid in item_ids:
            vals = [run[iid][m] for run in runs if run.get(iid, {}).get(m) is not None]
            if vals:
                max_vals.append(max(vals))
                mean_vals.append(statistics.mean(vals))
        out[m] = {
            "max_of_n": _mean(max_vals),
            "mean_of_n": _mean(mean_vals),
            "n_items": len(max_vals),
        }
    # duration：把所有 run 的 per-item duration 摊平算 P50/P95
    durations = [
        float(run[iid]["duration_ms"]) for run in runs for iid in run if run[iid].get("duration_ms")
    ]
    out["duration_ms"] = {
        "p50": _pct(durations, 0.5),
        "p95": _pct(durations, 0.95),
        "n": len(durations),
    }
    return out


def _delta(cur: dict, baseline_summary: dict) -> dict:
    """on − off 的 delta（主指标 mean_of_n + p95）。baseline_summary = off 趟的 summary.json。"""
    base = baseline_summary.get("aggregate", {})
    d: dict = {}
    for m in (
        "ragas_context_recall",
        "fact_coverage",
        "ragas_faithfulness",
        "ragas_answer_relevance",
        "ragas_context_precision",
        "context_recall_section",
        "context_recall_spec",
        "negative_pass",
    ):
        cur_v = (cur.get(m) or {}).get("mean_of_n")
        base_v = (base.get(m) or {}).get("mean_of_n")
        if cur_v is not None and base_v is not None:
            d[m] = round(cur_v - base_v, 4)
    cur_p95 = (cur.get("duration_ms") or {}).get("p95")
    base_p95 = (base.get("duration_ms") or {}).get("p95")
    if cur_p95 is not None and base_p95 is not None:
        d["p95_ms"] = round(cur_p95 - base_p95, 1)
    return d


async def main() -> None:
    base_url = os.environ["EVAL_BACKEND_BASE_URL"].rstrip("/")
    token = os.environ["EVAL_BACKEND_TOKEN"]
    n_runs = int(os.environ.get("N_RUNS", "2"))
    mode = os.environ.get("SMALL2BIG_MODE", "on")

    golden_path, ids = build_subset()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    outdir = REPO / "eval-results" / f"small2big-ablation-{mode}-{ts}"
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"[s2b-eval] mode={mode} subset n={len(ids)} ids={ids}")
    print(f"[s2b-eval] base_url={base_url} n_runs={n_runs} outdir={outdir}")

    runs: list[dict[str, dict]] = []
    for i in range(n_runs):
        t0 = time.time()
        run_map = await one_run(golden_path, base_url, token)
        dt = time.time() - t0
        runs.append(run_map)
        per_run = {
            m: _mean([v[m] for v in run_map.values() if v.get(m) is not None])
            for m in QUALITY_METRICS
        }
        print(
            f"[s2b-eval] run {i + 1}/{n_runs} done in {dt:.0f}s  {json.dumps(per_run, ensure_ascii=False)}"
        )
        (outdir / f"run{i + 1}.json").write_text(
            json.dumps(run_map, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    agg = aggregate(runs)
    summary = {
        "ts": ts,
        "mode": mode,
        "n_runs": n_runs,
        "n_items": len(ids),
        "item_ids": ids,
        "aggregate": agg,
    }

    compare_to = os.environ.get("ABLATION_COMPARE_TO", "").strip()
    if compare_to and Path(compare_to).exists():
        baseline = json.loads(Path(compare_to).read_text(encoding="utf-8"))
        summary["compare_to"] = compare_to
        summary["delta_vs_baseline"] = _delta(agg, baseline)

    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[s2b-eval] ===== SUMMARY =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[s2b-eval] summary → {outdir / 'summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
