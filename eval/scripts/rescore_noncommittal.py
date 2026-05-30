"""对一组指定 item_ids，仅用最新 build_default_ragas_scorer（含 noncommittal discount）
重打 ragas_answer_relevance。其他 metric 不动。结果以 patch 形式打印 + 可选写入。

用法：
    cd /data/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.rescore_noncommittal \\
        --src eval-results/v10-merge-max-2026-05-30/results.json \\
        --golden eval/golden/v1.yaml \\
        --bm25-dir /data/tgpp/bm25/voyage \\
        --item-ids hand-multi-003,hand-table-005,hand-table-007,hand-formula-006 \\
        --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from eval.ragas_eval import _extract_contexts, build_default_ragas_scorer
from eval.runner_retrieval import load_golden
from eval.scripts.rejudge_results import (
    _build_agent_response,
    _hydrate_citations,
    build_chunk_content_index,
)
from eval.settings import EvalSettings

log = logging.getLogger("rescore_nc")


async def _score_ans_rel(scorer, item, resp) -> float | None:
    """跑单题单 metric (answer_relevancy with discount)。"""
    from datasets import Dataset
    from ragas import evaluate

    contexts = _extract_contexts(resp)
    if not contexts or not resp.answer:
        return None
    # 取出 scorer.metrics 中的 answer_relevancy（即 _MajorityVoteAnswerRelevancy）
    ar_metric = None
    for m in scorer.metrics:
        if getattr(m, "name", "") == "answer_relevancy":
            ar_metric = m
            break
    if ar_metric is None:
        raise RuntimeError("answer_relevancy metric not found in scorer")

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
            metrics=[ar_metric],
            llm=scorer.llm,
            embeddings=scorer.embeddings,
            raise_exceptions=False,
            show_progress=False,
        )
        s = next(iter(ev.scores)) if ev.scores else {}
        v = s.get("answer_relevancy")
        if isinstance(v, (int, float)) and v == v:
            return float(v)
    except Exception as exc:
        log.warning("evaluate failed for %s: %s", item.id, exc)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--bm25-dir", type=Path, default=Path("/data/tgpp/bm25/voyage"))
    ap.add_argument("--item-ids", required=True, help="comma separated")
    ap.add_argument("--apply", action="store_true", help="write back to src (otherwise dry-run)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    ids = {s.strip() for s in args.item_ids.split(",") if s.strip()}
    doc = json.loads(args.src.read_text())
    items_by_id = {it.id: it for it in load_golden(args.golden)}

    targets = [r for r in doc["results"] if r["item_id"] in ids]
    if not targets:
        print("no matching items")
        return 1

    needed_specs = {
        c.get("spec_id") for r in targets for c in (r.get("citations") or []) if c.get("spec_id")
    }
    chunk_idx = build_chunk_content_index(
        args.bm25_dir, needed_specs={s for s in needed_specs if s}
    )

    settings = EvalSettings()
    scorer = build_default_ragas_scorer(settings)

    print(
        f"\n--- rescoring {len(targets)} items (answer_relevance with noncommittal discount) ---\n"
    )
    updates = {}
    for i, row in enumerate(targets, start=1):
        item = items_by_id[row["item_id"]]
        hydrated = _hydrate_citations(row.get("citations") or [], chunk_idx)
        resp = _build_agent_response(row, hydrated)
        old = row.get("ragas_answer_relevance")
        new = asyncio.run(_score_ans_rel(scorer, item, resp))
        updates[row["item_id"]] = new
        print(f"  [{i}/{len(targets)}] {row['item_id']:22s} old={old} → new={new}")

    if args.apply:
        for r in doc["results"]:
            if r["item_id"] in updates:
                r["ragas_answer_relevance"] = updates[r["item_id"]]
        # also recompute overall ragas_answer_relevance
        valid = [
            r.get("ragas_answer_relevance")
            for r in doc["results"]
            if isinstance(r.get("ragas_answer_relevance"), (int, float))
        ]
        if "overall" in doc:
            doc["overall"]["ragas_answer_relevance"] = (sum(valid) / len(valid)) if valid else None
        args.src.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"\nWROTE: {args.src}")
        if "overall" in doc:
            print(f"new overall ans_rel: {doc['overall'].get('ragas_answer_relevance')}")
    else:
        print("\n(dry-run; rerun with --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
