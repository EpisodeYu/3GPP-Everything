"""估算 formula_alt 改动的 chunk_id 漂移影响面（dry-run，不动索引）。

跑法：
    cd ingestion
    uv run --project . python scripts/poc_formula_alt_drift.py 38.211 38.214 38.212 38.213 38.331

输出（per spec + 汇总）：
    - chunks_total
    - chunks_with_annotation       （annotation 非空、content 变了 → chunk_id 变）
    - drift_pct                    （= 上一行 / total）
    - by_type 拆分（text / formula / table / asn1 / action_list / figure）
    - by_signal 拆分（symbols_only / stripped_only / both）

判读：
    - 漂移 ≤ 5%（M3→M6 过渡硬指标）：可直接 reindex，影响小
    - 漂移 5-15%：需要人 approve（说明 LaTeX/抽空 chunk 占比偏高，对应公式重 spec）
    - 漂移 > 15%：触发 CLAUDE.md §5.4，必须人审且最好分批 reindex

注意：本脚本只算"如果重新建索引会有多少 chunk 的 ID 变"。它不实际写任何索引，
不调 voyage embedding，不删 Qdrant。安全可重复跑。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from ingestion.chunker.builder import build_chunks
from ingestion.chunker.formula_alt import (
    build_formula_annotation,
    extract_latex_symbols,
    has_stripped_formula_marker,
)
from ingestion.hf_loader import (
    GsmaHfLoader,
    dedupe_keep_latest,
    get_meta,
    manifest_session,
    read_entries,
)

app = typer.Typer(no_args_is_help=True)


def _default_manifest_path() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


@app.command()
def main(
    spec_ids: list[str] = typer.Argument(..., help="spec_id 列表，如 38.211 38.331"),
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
) -> None:
    manifest_path = manifest or _default_manifest_path()
    if not Path(manifest_path).exists():
        typer.echo(f"manifest not found: {manifest_path}", err=True)
        raise typer.Exit(code=2)

    with manifest_session(manifest_path) as conn:
        all_entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")

    rows: list[dict] = []
    grand_total = 0
    grand_annotated = 0

    for spec_id in spec_ids:
        candidates = [e for e in all_entries if e.spec_id == spec_id]
        if not candidates:
            typer.echo(f"[skip] spec_id={spec_id} not in manifest", err=True)
            continue
        entry = dedupe_keep_latest(candidates)[0]

        loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN"))
        chunks_total = 0
        annotated = 0
        symbols_only = 0
        stripped_only = 0
        both = 0
        by_type: dict[str, dict[str, int]] = {}

        for bundle in loader.iter_specs([entry]):
            chunks, _ = build_chunks(bundle, vision_resolver=None)
            for c in chunks:
                chunks_total += 1
                bt = by_type.setdefault(c.chunk_type, {"total": 0, "annotated": 0})
                bt["total"] += 1
                # 重新看 piece 文本而不是 chunk content（content 已含 annotation）
                #   → 去掉 header + 末尾 annotation 后看 body 是否有信号
                body = _strip_header_and_annotation(c.content)
                has_sym = bool(extract_latex_symbols(body))
                has_strip = has_stripped_formula_marker(body)
                ann = build_formula_annotation(body)
                if ann:
                    annotated += 1
                    bt["annotated"] += 1
                    if has_sym and has_strip:
                        both += 1
                    elif has_sym:
                        symbols_only += 1
                    else:
                        stripped_only += 1

        rows.append(
            {
                "spec_id": spec_id,
                "chunks_total": chunks_total,
                "annotated": annotated,
                "drift_pct": (annotated / chunks_total * 100.0) if chunks_total else 0.0,
                "symbols_only": symbols_only,
                "stripped_only": stripped_only,
                "both": both,
                "by_type": by_type,
            }
        )
        grand_total += chunks_total
        grand_annotated += annotated

    typer.echo("")
    typer.echo("=== formula_alt drift estimate (dry-run, no indexing) ===")
    typer.echo("")
    typer.echo(
        f"{'spec_id':<10} {'total':>7} {'annot':>7} {'drift%':>7} "
        f"{'sym':>5} {'strip':>6} {'both':>5}"
    )
    typer.echo("-" * 56)
    for r in rows:
        typer.echo(
            f"{r['spec_id']:<10} {r['chunks_total']:>7} {r['annotated']:>7} "
            f"{r['drift_pct']:>6.2f}% {r['symbols_only']:>5} "
            f"{r['stripped_only']:>6} {r['both']:>5}"
        )
    typer.echo("-" * 56)
    grand_pct = (grand_annotated / grand_total * 100.0) if grand_total else 0.0
    typer.echo(
        f"{'TOTAL':<10} {grand_total:>7} {grand_annotated:>7} {grand_pct:>6.2f}%"
    )

    typer.echo("\nBy chunk_type (annotated / total)：")
    type_agg: dict[str, dict[str, int]] = {}
    for r in rows:
        for t, d in r["by_type"].items():
            agg = type_agg.setdefault(t, {"total": 0, "annotated": 0})
            agg["total"] += d["total"]
            agg["annotated"] += d["annotated"]
    for t in sorted(type_agg):
        d = type_agg[t]
        pct = (d["annotated"] / d["total"] * 100.0) if d["total"] else 0.0
        typer.echo(f"  {t:<15} {d['annotated']:>5} / {d['total']:>5}  ({pct:>5.2f}%)")


def _strip_header_and_annotation(content: str) -> str:
    """脱掉 chunk content 的头（`[<spec_id> § ...]\\n\\n`）+ 末尾 annotation。

    用于回看原 piece body：annotation 行（Formula symbols / stripped note）若在
    末尾就剥掉。否则函数会因看到自己的 annotation 而双重统计。
    """
    lines = content.splitlines()
    # 去 header（第一行 `[...]`）
    if lines and lines[0].startswith("[") and lines[0].endswith("]"):
        lines = lines[1:]
        # header 后通常跟空行
        while lines and not lines[0].strip():
            lines = lines[1:]
    # 去 trailing annotation
    while lines and (
        lines[-1].startswith("Formula symbols:")
        or lines[-1].startswith("[Note: source markdown")
        or not lines[-1].strip()
    ):
        lines.pop()
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(app())
