"""对 rejudge 结果里 ragas 超时丢样本（faithfulness=None）的**非 negative** 题重打 ragas。

为什么需要：默认 ragas RunConfig per-job timeout=180s，长答案 faithfulness 拆很多
statement → 易 TimeoutError → 该题 4 metric 全 None，被踢出均值。本脚本用更长 timeout
+ 受控并发重打这些题，就地合并回 results.json 并重算 overall / by_category 聚合。

negative 题 faithfulness 本就 N/A（无 ground truth），不重试。empty-context skip 题
（citation 没 hydrate 出 content）重试也救不回，会再次 skip（快速、无 LLM 调用）。

用法：
    cd /data/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.retry_failed_ragas \\
            --results eval-results/2026-05-27-rejudge-after/results.json \\
            --golden eval/golden/v1.yaml \\
            --bm25-dir /data/tgpp/bm25/voyage \\
            --timeout 600 --workers 4
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

from eval.ragas_eval import build_default_ragas_scorer
from eval.runner_retrieval import load_golden
from eval.scripts.rejudge_results import (
    _aggregate,
    _build_agent_response,
    _hydrate_citations,
    _negative_pass_rate,
    _write_report,
    build_chunk_content_index,
)
from eval.settings import EvalSettings

log = logging.getLogger("retry_ragas")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path, help="rejudge results.json（就地更新）")
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--bm25-dir", type=Path, default=Path("/data/tgpp/bm25/voyage"))
    ap.add_argument("--timeout", type=int, default=600, help="ragas per-job timeout 秒")
    ap.add_argument("--workers", type=int, default=4, help="ragas RunConfig max_workers")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    from ragas.run_config import RunConfig

    run_config = RunConfig(timeout=args.timeout, max_workers=args.workers)

    doc = json.loads(args.results.read_text())
    rows = doc["results"]
    items_by_id = {it.id: it for it in load_golden(args.golden)}

    # 目标：非 negative 且 faithfulness=None 的题
    targets = [
        r
        for r in rows
        if r.get("ragas_faithfulness") is None
        and (items_by_id.get(r["item_id"]) and items_by_id[r["item_id"]].category != "negative")
    ]
    print(f"待重试（非 neg 的 ragas None）：{len(targets)} 题")
    if not targets:
        return 0

    needed_specs = {
        c.get("spec_id")
        for r in targets
        for c in (r.get("citations") or [])
        if c.get("spec_id")
    }
    chunk_idx = build_chunk_content_index(args.bm25_dir, needed_specs={s for s in needed_specs if s})

    settings = EvalSettings()
    scorer = build_default_ragas_scorer(settings)

    recovered, still_none, empty_ctx = 0, 0, 0
    for i, row in enumerate(targets, start=1):
        item = items_by_id[row["item_id"]]
        hydrated = _hydrate_citations(row.get("citations") or [], chunk_idx)
        resp = _build_agent_response(row, hydrated)
        try:
            scores = scorer.score_item(item, resp, run_config=run_config)
        except Exception as exc:  # noqa: BLE001
            log.warning("retry score_item failed item=%s: %s", row["item_id"], exc)
            scores = {}
        new_faith = scores.get("ragas_faithfulness")
        if new_faith is not None:
            row.update(scores)
            recovered += 1
            tag = "OK"
        else:
            # 区分：有 hydrate 出 content 但仍 None（仍超时）vs 真空 context
            has_ctx = any(c.get("content") for c in hydrated)
            if has_ctx:
                still_none += 1
                tag = "STILL-TIMEOUT"
            else:
                empty_ctx += 1
                tag = "EMPTY-CTX"
        print(f"  [{i}/{len(targets)}] {row['item_id']:18} {tag} faith={new_faith}")

    # 重算聚合（结构与 rejudge 一致）
    doc["overall"] = _aggregate(rows)
    src_b: dict[str, list] = defaultdict(list)
    cat_b: dict[str, list] = defaultdict(list)
    for r in rows:
        it = items_by_id.get(r["item_id"])
        if not it:
            continue
        src_b[it.source].append(r)
        cat_b[it.category].append(r)
    doc["by_source"] = {s: _aggregate(rs) for s, rs in src_b.items()}
    doc["by_category"] = {c: _aggregate(rs) for c, rs in cat_b.items()}
    doc["negative_summary"] = _negative_pass_rate(rows)

    args.results.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str))
    _write_report(args.results.parent, doc["overall"], doc["by_source"], doc["by_category"], doc["negative_summary"])

    valid = sum(1 for r in rows if r.get("ragas_faithfulness") is not None)
    print(
        f"\n重试完成：救回 {recovered} | 仍超时 {still_none} | 空context {empty_ctx}\n"
        f"faithfulness 有效样本：{valid}/{len(rows)}；overall faith={doc['overall'].get('ragas_faithfulness')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
