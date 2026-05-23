"""清理 Langfuse 陈旧 traces + push 最新 dataset/run。

操作分三步（每步独立 flag 控制，避免误删）：

1. **清陈旧 traces**（`--clean-traces`）：删 Cloud 上当前 project 全部 trace。
   Langfuse v4 SDK 没有"按 dataset 过滤"删除接口，只能列全部再删；本项目暂时
   就一个 dataset，全删即可。会同时清掉 built-in evaluators 自动产生的
   `Execute evaluator: ...` trace。

2. **upsert dataset 内容**（`--push-dataset`）：用 push_golden_to_langfuse
   覆盖 `tgpp-golden-v1` 的 175 条 item.input/expected_output（M7.7 改了 facts/forbidden
   + M7.7+ 改了 specs，老 dataset 内容已经过期）。SDK 文档：create_dataset_item
   按 id upsert。

3. **push 新 run score**（`--push-run`）：读 eval-results/m8-baseline/results.json
   每条用 (run_label, item_id) 生成幂等 trace_id + create_score。

用法：

    # 一次性做 1+2+3
    uv run --project eval python -m eval.scripts.langfuse_cleanup_and_push \\
        --clean-traces --push-dataset --push-run \\
        --results eval-results/m8-baseline/results.json \\
        --golden eval/golden/v1.yaml \\
        --run-label m8-baseline-2026-05-24

    # 仅 dry-run：列要做的事，不动 Cloud
    uv run --project eval python -m eval.scripts.langfuse_cleanup_and_push \\
        --dry-run --clean-traces --push-dataset --push-run \\
        --results eval-results/m8-baseline/results.json \\
        --golden eval/golden/v1.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from eval.langfuse_dataset import (
    get_client,
    push_golden_to_langfuse,
    push_run_score,
)
from eval.runner import (
    AgentResponse,
    _emit_langfuse_experiment_span,
    _link_trace_to_dataset_run,
    _resolve_dataset_id,
)
from eval.runner_retrieval import load_golden
from eval.settings import EvalSettings

log = logging.getLogger("lf_cleanup")


def _list_all_trace_ids(cli) -> list[str]:
    """列 project 下所有 trace_id（分页）。"""
    out: list[str] = []
    page = 1
    while True:
        resp = cli.api.trace.list(limit=100, page=page)
        for tr in resp.data:
            out.append(tr.id)
        if page >= (resp.meta.total_pages or 1):
            break
        page += 1
    return out


def _clean_traces(cli, *, dry_run: bool) -> int:
    ids = _list_all_trace_ids(cli)
    log.info("found %d traces to delete", len(ids))
    if dry_run:
        return len(ids)
    # delete_multiple 一次最多 100; 拆批
    deleted = 0
    BATCH = 100
    for i in range(0, len(ids), BATCH):
        batch = ids[i : i + BATCH]
        try:
            cli.api.trace.delete_multiple(trace_ids=batch)
            deleted += len(batch)
            log.info("deleted batch %d-%d", i, i + len(batch))
        except Exception as exc:  # pragma: no cover
            log.warning("delete batch failed: %s", exc)
    try:
        cli.flush()
    except Exception as exc:
        log.warning("flush after delete failed: %s", exc)
    return deleted


def _push_dataset(golden_path: Path, *, dry_run: bool) -> int:
    if dry_run:
        # Show count only
        import yaml

        with golden_path.open() as f:
            n = len(yaml.safe_load(f).get("items") or [])
        log.info("dry-run: would push %d items to tgpp-golden-v1", n)
        return n
    return push_golden_to_langfuse(
        golden_path,
        dataset_name="tgpp-golden-v1",
        description="3GPP-Everything golden v1 (M7.7+ facts/forbidden/spec_attribution 修正后)",
    )


def _negative_pass_score(verdict: str | None) -> float | None:
    return {
        "VALID_REFUSAL": 1.0,
        "PARTIAL_REFUSAL": 0.5,
        "INVALID": 0.0,
    }.get(verdict or "")


def _push_run(
    cli,
    results_path: Path,
    *,
    golden_path: Path,
    run_label: str,
    dataset_name: str = "tgpp-golden-v1",
    dry_run: bool,
) -> int:
    """创建真 trace + span + dataset_run_item + scores（runner.py 同一管线复用）。

    旧实现 (`make_eval_trace_id` + `push_run_score`) 生成 seed-based 假 trace_id
    但不创建 trace 实体，Cloud 收到的 score 是 orphan → 全部过滤丢弃。
    """
    with results_path.open() as f:
        data = json.load(f)
    rows = data.get("results") or []
    items = {it.id: it for it in load_golden(golden_path)}
    log.info("pushing %d trace+scores for run_label=%s", len(rows), run_label)

    dataset_id = _resolve_dataset_id(cli, dataset_name) if not dry_run else None
    pushed = 0
    for r in rows:
        gid = r["item_id"]
        item = items.get(gid)
        if not item:
            log.warning("item %s missing in golden; skipping", gid)
            continue
        if dry_run:
            pushed += 1
            continue
        # Rehydrate AgentResponse for the experiment span input/output
        resp = AgentResponse(
            answer=r.get("answer", "") or "",
            citations=r.get("citations") or [],
            chunks_hit=[],
            chunks_rerank=[],
            terminal_event=r.get("terminal_event", "final"),
            duration_ms=int(r.get("duration_ms") or 0),
        )
        emit = _emit_langfuse_experiment_span(
            cli,
            item=item,
            resp=resp,
            dataset_name=dataset_name,
            dataset_id=dataset_id,
            run_label=run_label,
        )
        if not emit:
            continue
        trace_id, _obs_id = emit
        _link_trace_to_dataset_run(
            cli,
            trace_id=trace_id,
            dataset_name=dataset_name,
            dataset_item_id=gid,
            run_label=run_label,
        )
        push_run_score(
            trace_id,
            {
                "context_recall_spec": r.get("context_recall_spec"),
                "context_recall_section": r.get("context_recall_section"),
                "fact_coverage": r.get("fact_coverage"),
                "forbidden_violation": 1.0 if r.get("forbidden_violations") else 0.0,
                "ragas_faithfulness": r.get("ragas_faithfulness"),
                "ragas_answer_relevance": r.get("ragas_answer_relevance"),
                "ragas_context_recall": r.get("ragas_context_recall"),
                "ragas_context_precision": r.get("ragas_context_precision"),
                "negative_judge_pass": _negative_pass_score(r.get("negative_judge_verdict")),
                "latency_ms": r.get("duration_ms"),
            },
            comment="M8 baseline (judge=deepseek-v4-pro for ragas; mimo-v2.5-pro for negative; golden=M7.7+)",
            metadata={
                "item_id": gid,
                "category": r.get("category"),
                "language": r.get("language"),
                "run_label": run_label,
                "_source": r.get("_source"),
            },
            client=cli,
        )
        pushed += 1
        if pushed % 25 == 0:
            log.info("pushed %d/%d", pushed, len(rows))
    try:
        cli.flush()
    except Exception as exc:
        log.warning("final flush failed: %s", exc)
    return pushed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean-traces", action="store_true")
    ap.add_argument("--push-dataset", action="store_true")
    ap.add_argument("--push-run", action="store_true")
    ap.add_argument("--golden", type=Path)
    ap.add_argument("--results", type=Path)
    ap.add_argument("--run-label", default="m8-baseline-2026-05-24")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = EvalSettings()
    cli = get_client(settings)
    if cli is None:
        print("ERROR: Langfuse client unavailable (missing keys?)", file=sys.stderr)
        return 2

    actions_done = []
    if args.clean_traces:
        n = _clean_traces(cli, dry_run=args.dry_run)
        actions_done.append(f"clean_traces: {n} traces {'(dry-run)' if args.dry_run else 'deleted'}")
        # be polite to Cloud: small pause before next big batch op
        if not args.dry_run:
            time.sleep(2)
    if args.push_dataset:
        if not args.golden:
            print("ERROR: --push-dataset requires --golden", file=sys.stderr)
            return 2
        n = _push_dataset(args.golden, dry_run=args.dry_run)
        actions_done.append(
            f"push_dataset: {n} items {'(dry-run)' if args.dry_run else 'upserted'}"
        )
        if not args.dry_run:
            time.sleep(2)
    if args.push_run:
        if not args.results or not args.golden:
            print("ERROR: --push-run requires --results and --golden", file=sys.stderr)
            return 2
        n = _push_run(
            cli,
            args.results,
            golden_path=args.golden,
            run_label=args.run_label,
            dry_run=args.dry_run,
        )
        actions_done.append(
            f"push_run: {n} traces+scores {'(dry-run)' if args.dry_run else 'pushed'}"
        )
    if not actions_done:
        print("nothing to do (pass --clean-traces / --push-dataset / --push-run)")
    for a in actions_done:
        print(f"  ✓ {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
