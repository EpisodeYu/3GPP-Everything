"""eval CLI 入口。

子命令分组：
- `teleqna pull`   — clone GitHub + 解压 + parse → raw.jsonl
- `teleqna filter` — raw.jsonl → filtered.jsonl + out_of_scope.jsonl + stats
- `retrieval smoke` — 对一条 query 跑 retrieve（验证 LiteLLM + Qdrant 可达）

完整 retrieval 评测脚本另立 `eval/scripts/`。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from pathlib import Path

import typer

from eval.teleqna.filter import DEFAULT_CATEGORIES_KEEP, filter_jsonl
from eval.teleqna.infer import (
    DEFAULT_CONCURRENT,
    DEFAULT_RPM,
    build_default_client,
    infer_batch_async,
)
from eval.teleqna.pull import (
    DEFAULT_DATA_DIR,
    DEFAULT_RAW_JSONL,
    DEFAULT_REPO_URL,
    pull_all,
)

app = typer.Typer(no_args_is_help=True, help="tgpp-eval CLI")
teleqna_app = typer.Typer(no_args_is_help=True, help="TeleQnA pull / filter / infer")
retrieval_app = typer.Typer(no_args_is_help=True, help="retrieval smoke / batch")
builder_app = typer.Typer(no_args_is_help=True, help="MCQ→开放问答 LLM 转化 (T2)")
golden_app = typer.Typer(no_args_is_help=True, help="金标准 YAML 校验 / 合并 (M7.0)")
app.add_typer(teleqna_app, name="teleqna")
app.add_typer(retrieval_app, name="retrieval")
app.add_typer(builder_app, name="builder")
app.add_typer(golden_app, name="golden")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


@teleqna_app.command("pull")
def teleqna_pull(
    repo_url: str = typer.Option(DEFAULT_REPO_URL, "--repo-url"),
    data_dir: Path = typer.Option(DEFAULT_DATA_DIR, "--data-dir"),
    skip_existing: bool = typer.Option(True, "--skip-existing/--force"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """clone TeleQnA repo + 解压 + parse → raw.jsonl"""
    _setup_logging(verbose)
    report = pull_all(repo_url=repo_url, data_dir=data_dir, skip_existing=skip_existing)
    typer.echo(json.dumps({k: str(v) for k, v in report.items()}, indent=2))


@teleqna_app.command("filter")
def teleqna_filter(
    raw_jsonl: Path = typer.Option(DEFAULT_RAW_JSONL, "--raw"),
    out_dir: Path = typer.Option(DEFAULT_DATA_DIR / "filtered", "--out-dir"),
    keep_overview: bool = typer.Option(True, "--keep-overview/--strict"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """raw.jsonl → filtered.jsonl + out_of_scope.jsonl + stats（17 篇 whitelist 硬约束）"""
    _setup_logging(verbose)
    cats = DEFAULT_CATEGORIES_KEEP if keep_overview else frozenset({"Standards specifications"})
    stats = filter_jsonl(raw_jsonl=raw_jsonl, out_dir=out_dir, categories_keep=cats)
    typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))


@teleqna_app.command("infer")
def teleqna_infer(
    raw_jsonl: Path = typer.Option(DEFAULT_RAW_JSONL, "--raw"),
    out_jsonl: Path = typer.Option(DEFAULT_DATA_DIR / "llm_inferred.jsonl", "--out"),
    include_categories: list[str] = typer.Option(
        ["Standards specifications", "Standards overview"],
        "--include-category",
        help="可重复；只对这些 category 跑 LLM 推断",
    ),
    skip_already_kept: bool = typer.Option(
        True,
        "--skip-already-kept/--include-already-kept",
        help="跳过 explanation 里已硬命中 17 篇 spec 的题（filter.kept），省 token",
    ),
    limit: int = typer.Option(0, "--limit", help="0 = 全跑；>0 = 只跑前 N 题（spike 用）"),
    rpm: int = typer.Option(DEFAULT_RPM, "--rpm"),
    concurrent: int = typer.Option(DEFAULT_CONCURRENT, "--concurrent"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """LLM 辅助 spec 推断（mimo-v2.5）：对 Standards 类无明确 spec 引用的题跑推断"""
    import asyncio
    import json as _json

    from eval.teleqna.filter import filter_one

    _setup_logging(verbose)
    cats = frozenset(include_categories)
    candidates: list[dict] = []
    skipped_already_kept = 0
    skipped_other_category = 0
    with raw_jsonl.open("r", encoding="utf-8") as f_in:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            item = _json.loads(line)
            if str(item.get("category", "")) not in cats:
                skipped_other_category += 1
                continue
            if skip_already_kept:
                verdict, _ = filter_one(item)
                if verdict == "kept":
                    skipped_already_kept += 1
                    continue
            candidates.append(item)
    if limit > 0:
        candidates = candidates[:limit]

    typer.echo(
        f"infer scope: raw={raw_jsonl} | candidates={len(candidates)} | "
        f"skipped_already_kept={skipped_already_kept} | "
        f"skipped_other_category={skipped_other_category} | "
        f"rpm={rpm} concurrent={concurrent} limit={limit}"
    )

    async def _go() -> None:
        client = build_default_client()
        try:
            stats = await infer_batch_async(
                candidates,
                out_path=out_jsonl,
                client=client,
                rpm=rpm,
                concurrent=concurrent,
            )
            typer.echo(_json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        finally:
            await client.aclose()

    asyncio.run(_go())


@retrieval_app.command("smoke")
def retrieval_smoke(
    query: str = typer.Argument(..., help="自然语言 query"),
    dim: int = typer.Option(2048, "--dim"),
    top_k: int = typer.Option(5, "--top-k"),
    spec_filter: list[str] = typer.Option(None, "--spec", help="可重复，仅在 spec 内召回"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """跑一次 retrieval（验证 LiteLLM + Qdrant + 17 篇索引可达）"""
    _setup_logging(verbose)
    from eval.retrieval import Retriever

    with Retriever() as r:
        hits = r.search(query, dim=dim, top_k=top_k, spec_filter=spec_filter or None)

    typer.echo(f"got {len(hits)} hits (dim={dim}, top_k={top_k}):")
    for i, h in enumerate(hits, start=1):
        sec = ".".join(h.section_path) if h.section_path else "(no-sec)"
        preview = h.content.replace("\n", " ")[:140]
        typer.echo(
            f"  #{i} score={h.score:.4f} spec={h.spec_id} {h.chunk_type} " f"sec={sec} | {preview}…"
        )


@retrieval_app.command("decide")
def retrieval_decide(
    golden_yaml: Path = typer.Option(Path(__file__).parent / "golden" / "v1.yaml", "--golden"),
    out_dir: Path = typer.Option(
        Path(__file__).parent.parent / "eval-results" / "m3-embedding-poc",
        "--out-dir",
    ),
    dims: str = typer.Option("2048,1024", "--dims"),
    top_k: int = typer.Option(20, "--top-k"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """T4+T5 维度决胜：跑 d=2048 vs d=1024 retrieval-only 评测，输出报告 + 决胜结论"""
    import json as _json
    from datetime import datetime

    from eval.runner_retrieval import (
        decide_winner,
        evaluate_retrieval,
        load_golden,
        write_report_markdown,
    )

    _setup_logging(verbose)
    dim_list = tuple(int(x.strip()) for x in dims.split(",") if x.strip())
    golden = load_golden(golden_yaml)
    typer.echo(f"loaded golden: {len(golden)} items from {golden_yaml}")

    by_dim, rows = evaluate_retrieval(golden, dims=dim_list, top_k=top_k)
    verdict = decide_winner(by_dim)

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_ts = out_dir / ts
    write_report_markdown(
        out_dir=out_ts,
        by_dim=by_dim,
        verdict=verdict,
        rows=rows,
        golden_path=golden_yaml,
    )
    typer.echo(_json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2))
    typer.echo(f"\n→ report dir: {out_ts}")


@builder_app.command("transform")
def builder_transform(
    candidates_jsonl: Path = typer.Option(
        DEFAULT_DATA_DIR / "filtered" / "filtered.jsonl",
        "--candidates",
        help="输入：filtered.jsonl 或 llm_inferred.jsonl，过滤 inferred_specs ∈ whitelist 的题",
    ),
    out_yaml: Path = typer.Option(Path(__file__).parent / "golden" / "v1.draft.yaml", "--out"),
    limit: int = typer.Option(0, "--limit", help="0=全跑，>0=只跑前 N 题 spike 用"),
    rpm: int = typer.Option(50, "--rpm"),
    concurrent: int = typer.Option(10, "--concurrent"),
    min_confidence: str = typer.Option(
        "medium",
        "--min-confidence",
        help="只跑 LLM 推断 confidence >= 此档的题（high|medium|low）",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """T2 LLM 转化（mimo-v2.5-pro）：把 MCQ 候选 → 开放问答 v1.draft.yaml"""
    import asyncio
    import json as _json

    from eval.builder.transform import build_transform_client, transform_batch_async

    _setup_logging(verbose)

    confidence_order = {"high": 0, "medium": 1, "low": 2}
    threshold = confidence_order.get(min_confidence.lower(), 1)

    candidates: list[dict] = []
    skipped_no_spec = 0
    skipped_low_conf = 0
    for line in candidates_jsonl.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        item = _json.loads(line)
        # 接受两种来源：filtered.jsonl (expected_specs_inferred) / llm_inferred.jsonl (llm_in_whitelist)
        in_white = item.get("llm_in_whitelist") or item.get("expected_specs_inferred") or []
        if not in_white:
            skipped_no_spec += 1
            continue
        conf = str(item.get("llm_confidence") or "high").lower()  # filter.kept 默认 high
        if confidence_order.get(conf, 2) > threshold:
            skipped_low_conf += 1
            continue
        candidates.append(item)
    if limit > 0:
        candidates = candidates[:limit]

    typer.echo(
        f"transform scope: input={candidates_jsonl} | candidates={len(candidates)} | "
        f"skipped_no_spec={skipped_no_spec} | skipped_low_conf={skipped_low_conf} | "
        f"rpm={rpm} concurrent={concurrent} limit={limit}"
    )

    async def _go() -> None:
        client = build_transform_client()
        try:
            stats = await transform_batch_async(
                candidates,
                out_yaml=out_yaml,
                client=client,
                rpm=rpm,
                concurrent=concurrent,
            )
            typer.echo(_json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
        finally:
            await client.aclose()

    asyncio.run(_go())


@golden_app.command("validate")
def golden_validate(
    file: Path = typer.Option(..., "--file", "-f", exists=False, help="待校验的金标准 YAML 路径"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON 报告（CI 友好）"),
    strict_warnings: bool = typer.Option(
        False,
        "--strict-warnings",
        help="将 warning 也视为失败（exit code 1）",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """校验金标准 YAML（§3.5 schema）。

    - 必填字段 / 枚举值（category / language / source）/ id 全局唯一
    - negative 类约束（expected_specs 空 + must_say_not_found=true）
    - spec_id 形状（NN.NNN）warning + sections list[str]
    错误位置定位到 1-indexed 行号。

    退出码：0 = 全通过；1 = 至少一条 error（或 --strict-warnings 下任一 warning）。
    """
    _setup_logging(verbose)
    from eval.validators.golden import format_report, validate_golden_file

    report = validate_golden_file(file)
    if json_out:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(format_report(report))

    failed = (not report.ok) or (strict_warnings and bool(report.warnings))
    if failed:
        raise typer.Exit(code=1)


@golden_app.command("merge")
def golden_merge(
    inputs: list[Path] = typer.Option(
        ...,
        "--input",
        "-i",
        help="输入金标准 YAML，可重复；按出现顺序拼接 items",
    ),
    out: Path = typer.Option(..., "--out", "-o", help="合并输出路径（dry-run 时仅校验不写）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只跑校验和合并预演，不写文件"),
    force: bool = typer.Option(
        False, "--force", help="允许跨文件 id 冲突（后赢覆盖；默认 False 一旦冲突即 fail）"
    ),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON 报告"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """合并多个金标准 YAML → 单文件。

    前置：每个输入先过 `golden validate`，任一 invalid → fail 不进入合并。
    跨文件 id 唯一性硬约束（除非 --force），冲突时报双方 file:line 便于排查。
    顶层 sources / categories 取并集；total 重算；version / created_at 取第一个输入。
    """
    _setup_logging(verbose)
    from eval.validators.merger import format_merge_report, merge_golden_files

    report = merge_golden_files(inputs, out, dry_run=dry_run, force_overlap=force)
    if json_out:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(format_merge_report(report))
    if not report.ok:
        raise typer.Exit(code=1)


@golden_app.command("stats")
def golden_stats(
    file: Path = typer.Option(..., "--file", "-f", help="统计目标 YAML"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
    tolerance: int = typer.Option(5, "--tolerance", help="±N 容差视为 OK（默认 5）"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """统计 category / source / language 分布，对比 §3.4 目标（±tolerance 容差）。

    退出码：0 = 所有 category 在 ±tolerance 内；1 = 任一 GAP 或 OVER。
    """
    _setup_logging(verbose)
    from eval.validators.stats import compute_stats, format_stats

    stats = compute_stats(file, tolerance=tolerance)
    if json_out:
        typer.echo(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(format_stats(stats))
    if not stats.ok:
        raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
