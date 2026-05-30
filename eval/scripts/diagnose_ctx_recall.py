"""诊断脚本：对 v6 ragas 中 ctx_recall=0 但 section_recall=1.0 的几道题
直接抓 judge 的 statement-by-statement 分类，看到底为什么判 0。

非生产代码，验证完即删（或保留作 debug 工具）。

跑法：
    cd /data/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.diagnose_ctx_recall \\
            --results eval-results/v6-citation-index-20260529T092708Z-ragas/results.json \\
            --golden eval/golden/v1.yaml \\
            --bm25-dir /data/tgpp/bm25/voyage \\
            --item-ids hand-multi-002,hand-formula-001,hand-table-008
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from eval.ragas_eval import _extract_contexts, _ground_truth, build_default_ragas_scorer
from eval.runner import AgentResponse
from eval.runner_retrieval import load_golden
from eval.scripts.rejudge_results import (
    _build_agent_response,
    _hydrate_citations,
    build_chunk_content_index,
)
from eval.settings import EvalSettings

log = logging.getLogger("diagnose_ctx_recall")


async def _run_one(scorer, item, resp, *, label: str) -> None:
    """Manually run context_recall + see classifications."""
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import context_recall

    contexts = _extract_contexts(resp)
    ground_truth = _ground_truth(item)

    print(f"\n{'=' * 80}")
    print(f"== {label}  item={item.id}")
    print(f"== question (first 200): {item.question[:200]}")
    print(f"== ground_truth (full):  {ground_truth!r}")
    print(f"== contexts count: {len(contexts)}")
    for i, ctx in enumerate(contexts[:5]):
        print(f"   [{i}] {ctx[:300]}")
    if len(contexts) > 5:
        print(f"   ... ({len(contexts) - 5} more)")

    row = {
        "user_input": item.question,
        "response": resp.answer,
        "retrieved_contexts": contexts,
        "reference": ground_truth,
        # also dual keys for back-compat
        "question": item.question,
        "answer": resp.answer,
        "contexts": contexts,
        "ground_truth": ground_truth,
    }
    ds = Dataset.from_list([row])
    ev = evaluate(
        ds,
        metrics=[context_recall],
        llm=scorer.llm,
        embeddings=scorer.embeddings,
        raise_exceptions=False,
        show_progress=False,
    )
    # extract the score
    score_attr = getattr(ev, "scores", None)
    if score_attr is not None:
        first = next(iter(score_attr))
        print(f"==> ragas context_recall = {first}")
    df = ev.to_pandas()
    print(f"==> dataframe row: {df.iloc[0].to_dict()}")


def _build_resp(row: dict, chunk_idx: dict[str, str]) -> AgentResponse:
    hydrated = _hydrate_citations(row.get("citations") or [], chunk_idx)
    return _build_agent_response(row, hydrated)


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--bm25-dir", type=Path, default=Path("/data/tgpp/bm25/voyage"))
    ap.add_argument("--item-ids", default="hand-multi-002,hand-formula-001,hand-table-008")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s"
    )
    ids = [s.strip() for s in args.item_ids.split(",") if s.strip()]

    doc = json.loads(args.results.read_text())
    items_by_id = {it.id: it for it in load_golden(args.golden)}

    target_rows = [r for r in doc["results"] if r["item_id"] in ids]
    if not target_rows:
        print("no matching items found")
        return

    needed_specs = {
        c.get("spec_id")
        for r in target_rows
        for c in (r.get("citations") or [])
        if c.get("spec_id")
    }
    chunk_idx = build_chunk_content_index(
        args.bm25_dir, needed_specs={s for s in needed_specs if s}
    )

    settings = EvalSettings()
    scorer = build_default_ragas_scorer(settings)

    for r in target_rows:
        item = items_by_id[r["item_id"]]
        resp = _build_resp(r, chunk_idx)
        await _run_one(scorer, item, resp, label="ORIG_GT (space-joined facts)")
    print("\n\n=== DONE ===")


if __name__ == "__main__":
    asyncio.run(amain())
