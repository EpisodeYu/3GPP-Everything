"""Chunker CLI 子命令。

子命令：
- chunk         按 spec_id 加载并 chunk 单 spec → 打印统计 + 可选 JSONL 落盘
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path

import typer

from ingestion.hf_loader import (
    GsmaHfLoader,
    dedupe_keep_latest,
    get_meta,
    manifest_session,
    read_entries,
)

from .builder import ChunkParams, build_chunks, chunk_token_count
from .models import Chunk

app = typer.Typer(no_args_is_help=True, help="3GPP-Everything chunker CLI")
log = logging.getLogger(__name__)


def _default_manifest_path() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


def _hf_token() -> str | None:
    return os.environ.get("HF_TOKEN") or None


@app.command("chunk")
def chunk_cmd(
    spec_id: str = typer.Argument(..., help="spec_id，如 38.211"),
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
    out: Path = typer.Option(None, help="JSONL 输出路径，缺省则只打印统计"),
    inspect_clause: str = typer.Option(
        None, "--inspect", help="只打印某 clause 的 chunks（如 5.2.1），便于人审"
    ),
    target_tokens: int = typer.Option(250, help="目标 chunk token 数"),
    max_tokens: int = typer.Option(400, help="单 chunk token 上限"),
    overlap_tokens: int = typer.Option(50, help="相邻 paragraph chunk overlap token 数"),
    short_section_threshold: int = typer.Option(200, help="short sibling 合并阈值（token）"),
    sample_print: int = typer.Option(3, help="打印多少个 chunk 抽样到 stdout"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """加载单个 spec → chunk → 打印统计 + 可选 JSONL 落盘。

    依赖 hf-pull 已生成的 manifest；不会重新扫 HF 树。
    """
    logging.basicConfig(level=log_level)
    manifest_path = manifest or _default_manifest_path()
    if not Path(manifest_path).exists():
        raise typer.BadParameter(f"manifest not found: {manifest_path}. 先跑 hf-pull。")

    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")

    candidates = [e for e in entries if e.spec_id == spec_id]
    if not candidates:
        raise typer.BadParameter(f"spec_id {spec_id} 不在 manifest 中")
    entry = dedupe_keep_latest(candidates)[0]

    typer.echo(
        f"[chunk] spec_id={entry.spec_id} release={entry.release} "
        f"raw_md={entry.raw_md_size/1024:.1f}KiB images={entry.image_count}"
    )

    loader = GsmaHfLoader(revision=revision, token=_hf_token())
    params = ChunkParams(
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        short_section_threshold=short_section_threshold,
    )

    t0 = time.time()
    chunks: list[Chunk] = []
    stats = None
    for bundle in loader.iter_specs([entry]):
        chunks, stats = build_chunks(bundle, params=params, vision_resolver=None)
    elapsed = time.time() - t0

    if stats is None:
        raise typer.Exit(code=1)

    typer.echo(f"[chunk] elapsed={elapsed:.1f}s chunks={stats.chunks_total}")
    typer.echo(
        f"[chunk] sections total={stats.sections_total} kept={stats.sections_kept} "
        f"dropped={stats.sections_dropped} merged={stats.sections_merged}"
    )
    typer.echo(f"[chunk] chunks_by_type={stats.chunks_by_type}")
    typer.echo(f"[chunk] drop_reasons={stats.drop_reasons}")
    typer.echo(
        f"[chunk] figure_count={stats.figure_count} "
        f"figure_with_vision={stats.figure_with_vision}"
    )

    if inspect_clause:
        typer.echo(f"\n--- chunks for clause={inspect_clause} ---")
        for c in chunks:
            if c.clause == inspect_clause:
                _print_chunk_summary(c)

    if sample_print > 0:
        typer.echo(f"\n--- sample chunks (first {sample_print}) ---")
        for c in chunks[:sample_print]:
            _print_chunk_summary(c)

    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps(_chunk_to_json(c), ensure_ascii=False) + "\n")
        typer.echo(f"\n[chunk] wrote {len(chunks)} chunks → {out}")


def _print_chunk_summary(c: Chunk) -> None:
    tokens = chunk_token_count(c)
    head = c.content.replace("\n", " ")[:160]
    typer.echo(
        f"  [{c.chunk_type:<13}] clause={c.clause or '-':<10} tokens={tokens:>4} "
        f"chars={len(c.content):>5} | {head}{'...' if len(c.content) > 160 else ''}"
    )


def _chunk_to_json(c: Chunk) -> dict:
    d = asdict(c)
    d["section_path"] = list(d["section_path"])
    d["created_at"] = c.created_at.isoformat()
    return d
