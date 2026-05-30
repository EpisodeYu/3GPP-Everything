"""快速实验：对 v7 已有 results.json 的所有非 negative 题，用 ragas
`LLMContextPrecisionWithoutReference`（aka context_utilization）补算一遍，
看与 v7 当前的 ContextPrecision（with reference）对比，哪个更稳定 / 更高。

不重写 results.json；只输出对比表到 stdout。

跑法：
    cd /data/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.compute_utilization \\
        --results eval-results/v7-sent-gt-20260529T234652Z/results.json \\
        --golden eval/golden/v1.yaml \\
        --bm25-dir /data/tgpp/bm25/voyage
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from statistics import mean

from eval.ragas_eval import _extract_contexts, build_default_ragas_scorer
from eval.runner_retrieval import load_golden
from eval.scripts.rejudge_results import (
    _build_agent_response,
    _hydrate_citations,
    build_chunk_content_index,
)
from eval.settings import EvalSettings

log = logging.getLogger("util")


def score_one(scorer, item, resp) -> float | None:
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import LLMContextPrecisionWithoutReference

    contexts = _extract_contexts(resp)
    if not contexts or not resp.answer:
        return None
    metric = LLMContextPrecisionWithoutReference()
    row = {
        "user_input": item.question,
        "response": resp.answer,
        "retrieved_contexts": contexts,
        "question": item.question,
        "answer": resp.answer,
        "contexts": contexts,
    }
    try:
        ds = Dataset.from_list([row])
        ev = evaluate(
            ds,
            metrics=[metric],
            llm=scorer.llm,
            embeddings=scorer.embeddings,
            raise_exceptions=False,
            show_progress=False,
        )
        s = next(iter(ev.scores)) if ev.scores else {}
        for k in (
            "llm_context_precision_without_reference",
            "context_utilization",
        ):
            v = s.get(k)
            if isinstance(v, (int, float)) and v == v:
                return float(v)
    except Exception as exc:
        log.warning("evaluate failed for %s: %s", item.id, exc)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--bm25-dir", type=Path, default=Path("/data/tgpp/bm25/voyage"))
    ap.add_argument("--limit", type=int, default=0, help=">0 = 只跑前 N 题 (debug)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    doc = json.loads(args.results.read_text())
    items_by_id = {it.id: it for it in load_golden(args.golden)}
    rows = [r for r in doc["results"] if r.get("category") != "negative"]
    if args.limit > 0:
        rows = rows[: args.limit]

    needed_specs = {
        c.get("spec_id") for r in rows for c in (r.get("citations") or []) if c.get("spec_id")
    }
    chunk_idx = build_chunk_content_index(
        args.bm25_dir, needed_specs={s for s in needed_specs if s}
    )

    settings = EvalSettings()
    scorer = build_default_ragas_scorer(settings)

    print("\nitem_id              cat      old_prec    util      Δ")
    print("-" * 60)
    util_scores: list[float] = []
    old_scores: list[float] = []
    for _i, row in enumerate(rows, start=1):
        item = items_by_id[row["item_id"]]
        hydrated = _hydrate_citations(row.get("citations") or [], chunk_idx)
        resp = _build_agent_response(row, hydrated)
        old = row.get("ragas_context_precision")
        util = score_one(scorer, item, resp)
        if isinstance(util, (int, float)):
            util_scores.append(util)
        if isinstance(old, (int, float)):
            old_scores.append(old)
        delta = (util - old) if (isinstance(util, float) and isinstance(old, float)) else None
        print(
            (
                f"{item.id:20s} {item.category[:6]:6s}  "
                f"{old if isinstance(old, float) else '-':>6} -> {util if isinstance(util, float) else '-':>6}    "
                f"{'+' if (delta or 0) >= 0 else ''}{delta:.2f}"
                if delta is not None
                else f"{item.id:20s} {item.category[:6]:6s}  "
                f"{old if isinstance(old, float) else '-':>6} -> {util if isinstance(util, float) else '-':>6}"
            ),
            flush=True,
        )

    print("\n--- summary ---")
    if old_scores:
        print(
            f"ContextPrecision (with reference, SENT): n={len(old_scores)}, mean={mean(old_scores):.4f}"
        )
    if util_scores:
        print(
            f"ContextUtilization (without reference) : n={len(util_scores)}, mean={mean(util_scores):.4f}"
        )


if __name__ == "__main__":
    main()
