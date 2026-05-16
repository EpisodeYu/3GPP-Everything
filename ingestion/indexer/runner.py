"""Indexer CLI 子命令（embed / index / pipeline-hf / index-status / purge）。

CLI 风格与 hf_loader.runner / chunker.runner 一致：每个子命令独立 typer 命令，
顶层 ingestion/cli.py 把本模块的 app.registered_commands 挂过去。

子命令：

  embed <spec_id>           只跑 embedding（不写 Qdrant / BM25 / PG），打印统计
                            供 debug / 维度抽检 / 成本预估用
  index <spec_id>           单 spec 完整 indexer（chunker + embed + qdrant + bm25 + pg）
                            可加 `--no-vision` / `--skip-pg` 控制
  pipeline-hf               多 spec 批跑（默认走 manifest 全集 - 过滤白名单）
                            支持 `--limit` / `--spec-ids` / `--skip-indexed`
  index-status              输出 Qdrant collection point 数 / 各 spec count / BM25 meta
  purge-spec <spec_id>      清掉 Qdrant + BM25 + PG 中该 spec 的所有写入
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from pathlib import Path

import typer

from ingestion.chunker import ChunkParams
from ingestion.hf_loader import (
    GsmaHfLoader,
    SpecManifestEntry,
    dedupe_keep_latest,
    filter_ts_5g,
    get_meta,
    manifest_session,
    read_entries,
)
from ingestion.images import VisionResolver, build_resolver_from_env

from .bm25_writer import BM25Writer, default_bm25_dir
from .embedder import Embedder
from .models import Provider
from .pg_writer import PgChunkMetaWriter, default_database_url
from .pipeline import (
    IndexerComponents,
    index_spec,
    index_specs,
    index_stats_to_json,
    pipeline_stats_to_json,
)
from .qdrant_writer import QdrantWriter, collection_name_for_provider

app = typer.Typer(no_args_is_help=True, help="3GPP-Everything indexer CLI")
log = logging.getLogger(__name__)


def _default_manifest_path() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


def _hf_token() -> str | None:
    return os.environ.get("HF_TOKEN") or None


def _load_manifest_entries(manifest_path: Path) -> tuple[list[SpecManifestEntry], str | None]:
    if not Path(manifest_path).exists():
        raise typer.BadParameter(f"manifest not found: {manifest_path}. 先跑 hf-pull。")
    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")
    return entries, revision


def _pick_entries(
    all_entries: Iterable[SpecManifestEntry],
    *,
    spec_ids: list[str] | None,
    limit: int | None,
    only_whitelist: bool,
) -> list[SpecManifestEntry]:
    deduped = dedupe_keep_latest(list(all_entries))
    if only_whitelist:
        deduped = filter_ts_5g(deduped)
    if spec_ids:
        wanted = set(spec_ids)
        deduped = [e for e in deduped if e.spec_id in wanted]
    if limit and limit > 0:
        deduped = deduped[:limit]
    return deduped


def _resolve_vision(no_vision: bool) -> VisionResolver | None:
    if no_vision:
        return None
    try:
        return build_resolver_from_env()
    except Exception as exc:
        log.warning("vision resolver disabled (build failed: %s); figure chunks 用 GSMA 描述", exc)
        return None


# -------------------- embed --------------------


@app.command("embed")
def embed_cmd(
    spec_id: str = typer.Argument(..., help="spec_id，如 38.211"),
    provider: Provider = typer.Option("voyage", help="embedding provider: voyage / glm"),
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
    batch_size: int = typer.Option(64, help="单次 /embeddings 批大小"),
    sample_print: int = typer.Option(2, help="打印多少个 chunk 抽样（含向量前 4 维）"),
    no_vision: bool = typer.Option(
        True, help="不调 vision_resolver；只想验证 embedding 链路时用（默认 True 省钱）"
    ),
    log_level: str = typer.Option("INFO"),
) -> None:
    """对单 spec 跑 chunker + embedding，**不**写 Qdrant / BM25 / PG。

    用途：
    - 验证 LiteLLM 连通 / 模型 / 维度
    - 估算 token 消耗（× 全量 ≈ 总成本）
    - debug 单 spec chunk 内容
    """
    logging.basicConfig(level=log_level)
    manifest_path = manifest or _default_manifest_path()
    entries, revision = _load_manifest_entries(manifest_path)
    candidates = [e for e in entries if e.spec_id == spec_id]
    if not candidates:
        raise typer.BadParameter(f"spec_id {spec_id} 不在 manifest 中")
    entry = dedupe_keep_latest(candidates)[0]

    typer.echo(
        f"[embed] spec={entry.spec_id} provider={provider} "
        f"images={entry.image_count} raw_md={entry.raw_md_size/1024:.1f}KiB"
    )
    loader = GsmaHfLoader(revision=revision, token=_hf_token())
    vision = _resolve_vision(no_vision)

    from ingestion.chunker import build_chunks

    bundle = next(loader.iter_specs([entry]))
    chunks, _ = build_chunks(bundle, vision_resolver=vision)
    if not chunks:
        typer.echo("[embed] 0 chunks → exit")
        raise typer.Exit(code=1)

    t0 = time.time()
    with Embedder.from_env(provider=provider, batch_size=batch_size) as emb:
        result = emb.embed_texts([c.content for c in chunks])
    elapsed = time.time() - t0
    typer.echo(
        f"[embed] OK chunks={len(chunks)} dim={result.dim} "
        f"tokens={result.prompt_tokens} elapsed={elapsed:.1f}s model={result.model}"
    )
    for c, v in list(zip(chunks, result.vectors, strict=True))[:sample_print]:
        head = c.content.replace("\n", " ")[:120]
        typer.echo(
            f"  [{c.chunk_type:<13}] {c.chunk_id[:8]}... clause={c.clause or '-':<10} "
            f"vec[:4]={v[:4]} | {head}"
        )


# -------------------- index --------------------


@app.command("index")
def index_cmd(
    spec_id: str = typer.Argument(..., help="spec_id，如 38.331"),
    provider: Provider = typer.Option("voyage", help="embedding provider"),
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
    no_vision: bool = typer.Option(False, help="跳过 vision_resolver（M2 起默认启用）"),
    skip_pg: bool = typer.Option(False, help="跳过 PG chunks_meta 写入（DATABASE_URL 不可用时）"),
    purge_before: bool = typer.Option(
        True, help="写之前按 spec_id 删 Qdrant / PG / BM25 旧记录（plan §3 强幂等语义）"
    ),
    target_tokens: int = typer.Option(250),
    max_tokens: int = typer.Option(400),
    overlap_tokens: int = typer.Option(50),
    out: Path = typer.Option(None, help="可选 JSON 输出 IndexStats"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """单 spec 完整 indexer（chunker → embed → qdrant + bm25 + pg）。"""
    logging.basicConfig(level=log_level)
    manifest_path = manifest or _default_manifest_path()
    entries, revision = _load_manifest_entries(manifest_path)
    candidates = [e for e in entries if e.spec_id == spec_id]
    if not candidates:
        raise typer.BadParameter(f"spec_id {spec_id} 不在 manifest 中")
    entry = dedupe_keep_latest(candidates)[0]

    typer.echo(
        f"[index] spec={entry.spec_id} provider={provider} "
        f"images={entry.image_count} raw_md={entry.raw_md_size/1024:.1f}KiB"
    )
    loader = GsmaHfLoader(revision=revision, token=_hf_token())
    vision = _resolve_vision(no_vision)

    embedder = Embedder.from_env(provider=provider)
    qdrant = QdrantWriter(provider=provider)
    bm25 = BM25Writer(provider=provider)
    pg: PgChunkMetaWriter | None = None
    if not skip_pg and default_database_url():
        try:
            pg = PgChunkMetaWriter.from_env(provider=provider)
        except Exception as exc:
            typer.echo(f"[index] PG disabled: {exc}")
    components = IndexerComponents(
        embedder=embedder, qdrant=qdrant, bm25=bm25, pg=pg, vision_resolver=vision
    )

    bundle = next(loader.iter_specs([entry]))
    chunk_params = ChunkParams(
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )
    try:
        stats = index_spec(bundle, components, chunk_params=chunk_params, purge_before=purge_before)
    finally:
        components.close()

    if stats.error:
        typer.echo(f"[index] FAIL: {stats.error}")
        raise typer.Exit(code=1)

    typer.echo(
        f"[index] OK chunks={stats.chunks_total} dim={stats.vectors_dim} "
        f"tokens={stats.embedding_tokens} qdrant={stats.qdrant_upserted} "
        f"bm25={stats.bm25_persisted} pg={stats.pg_upserted} elapsed={stats.elapsed_s}s"
    )
    typer.echo(f"[index] chunks_by_type={stats.chunks_by_type}")
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(index_stats_to_json(stats), ensure_ascii=False, indent=2))
        typer.echo(f"[index] wrote stats → {out}")


# -------------------- pipeline-hf --------------------


@app.command("pipeline-hf")
def pipeline_hf_cmd(
    provider: Provider = typer.Option("voyage", help="embedding provider"),
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
    spec_ids: str = typer.Option(
        None, help="逗号分隔 spec_id 白名单（不填则跑 manifest 全集，过滤 TS 5G 白名单）"
    ),
    limit: int = typer.Option(0, help="最多跑多少篇（0 = 不限制）"),
    only_whitelist: bool = typer.Option(True, help="是否限定 TS + 5G 系列白名单（生产口径）"),
    skip_indexed: bool = typer.Option(
        False,
        help="若该 spec_id 在 Qdrant 已有 point > 0 → 跳过（增量续传用；新内容请关掉）",
    ),
    no_vision: bool = typer.Option(False, help="跳过 vision_resolver（dry-run / 预算紧张时用）"),
    skip_pg: bool = typer.Option(False, help="跳过 PG chunks_meta（DATABASE_URL 不可用时）"),
    purge_before: bool = typer.Option(
        True, help="每篇 spec 写之前按 spec_id 清旧记录（plan §3 强幂等语义）"
    ),
    progress_every: int = typer.Option(1, help="每 N 篇打印一次进度（0 = 关闭）"),
    out: Path = typer.Option(None, help="可选 JSON 输出 PipelineStats"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """多 spec 批跑（M2 维度 ablation / M6 全量入口）。

    典型用法：

      ingestion pipeline-hf --provider voyage --spec-ids 38.211,38.331 --no-vision
      ingestion pipeline-hf --provider voyage --limit 20                   # 20 篇 POC
      ingestion pipeline-hf --provider voyage                              # 全量
    """
    logging.basicConfig(level=log_level)
    manifest_path = manifest or _default_manifest_path()
    entries, revision = _load_manifest_entries(manifest_path)

    wanted: list[str] | None = (
        [s.strip() for s in spec_ids.split(",") if s.strip()] if spec_ids else None
    )
    picked = _pick_entries(
        entries, spec_ids=wanted, limit=limit if limit > 0 else None, only_whitelist=only_whitelist
    )
    if not picked:
        raise typer.BadParameter("no spec matched filters")

    typer.echo(
        f"[pipeline-hf] provider={provider} specs={len(picked)} "
        f"vision={'off' if no_vision else 'on'} skip_indexed={skip_indexed}"
    )

    loader = GsmaHfLoader(revision=revision, token=_hf_token())
    vision = _resolve_vision(no_vision)

    embedder = Embedder.from_env(provider=provider)
    qdrant = QdrantWriter(provider=provider)
    bm25 = BM25Writer(provider=provider)
    pg: PgChunkMetaWriter | None = None
    if not skip_pg and default_database_url():
        try:
            pg = PgChunkMetaWriter.from_env(provider=provider)
        except Exception as exc:
            typer.echo(f"[pipeline-hf] PG disabled: {exc}")
    components = IndexerComponents(
        embedder=embedder, qdrant=qdrant, bm25=bm25, pg=pg, vision_resolver=vision
    )

    seen: list[int] = [0]

    def _progress(stats: object) -> None:
        seen[0] += 1
        if progress_every > 0 and seen[0] % progress_every == 0:
            s = stats  # type: ignore[assignment]
            typer.echo(
                f"  [{seen[0]}/{len(picked)}] {s.spec_id} "  # type: ignore[attr-defined]
                f"chunks={s.chunks_total} qdrant={s.qdrant_upserted} "  # type: ignore[attr-defined]
                f"tokens={s.embedding_tokens} {s.elapsed_s}s "  # type: ignore[attr-defined]
                f"{'OK' if s.succeeded else 'FAIL: '+(s.error or '')}"  # type: ignore[attr-defined]
            )

    try:
        bundles = loader.iter_specs(picked)
        pstats = index_specs(
            bundles,
            components,
            skip_indexed=skip_indexed,
            purge_before=purge_before,
            progress_cb=_progress,
        )
    finally:
        components.close()

    typer.echo(
        f"[pipeline-hf] DONE provider={pstats.provider} "
        f"attempted={pstats.specs_attempted} ok={pstats.specs_succeeded} "
        f"failed={pstats.specs_failed} chunks={pstats.chunks_total} "
        f"qdrant={pstats.qdrant_upserted} tokens={pstats.embedding_tokens} "
        f"elapsed={pstats.elapsed_s}s"
    )
    if pstats.failures:
        typer.echo("[pipeline-hf] failures:")
        for spec_id, err in pstats.failures:
            typer.echo(f"  - {spec_id}: {err}")
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(
            json.dumps(pipeline_stats_to_json(pstats), ensure_ascii=False, indent=2)
        )
        typer.echo(f"[pipeline-hf] wrote stats → {out}")


# -------------------- index-status --------------------


@app.command("index-status")
def index_status_cmd(
    provider: Provider = typer.Option("voyage"),
    spec_id: str | None = typer.Option(None, help="只看某 spec_id 的计数"),
) -> None:
    """Qdrant + BM25 + PG 状态查询。"""
    qdrant = QdrantWriter(provider=provider)
    bm25 = BM25Writer(provider=provider)
    pg: PgChunkMetaWriter | None = None
    if default_database_url():
        try:
            pg = PgChunkMetaWriter.from_env(provider=provider, schema_owner=False)
        except Exception as exc:
            typer.echo(f"[index-status] PG disabled: {exc}")

    typer.echo(f"[index-status] provider={provider}")
    if spec_id:
        typer.echo(f"  qdrant {qdrant.collection_name}: {qdrant.count(spec_id=spec_id)} points")
        if pg:
            typer.echo(f"  pg chunks_meta: {pg.count(spec_id=spec_id)} rows")
    else:
        try:
            typer.echo(f"  qdrant {qdrant.collection_name}: {qdrant.count()} total points")
        except Exception as exc:
            typer.echo(f"  qdrant N/A: {exc}")
        meta = bm25.read_meta()
        if meta:
            typer.echo(
                f"  bm25 dir={default_bm25_dir(provider)} "
                f"total={meta.get('total_chunks')} specs={meta.get('spec_count')}"
            )
        else:
            typer.echo(f"  bm25 dir={default_bm25_dir(provider)}（未 finalize）")
        if pg:
            typer.echo(f"  pg chunks_meta: {pg.count()} rows")
    typer.echo(f"  collection name: {collection_name_for_provider(provider)}")


# -------------------- purge-spec --------------------


@app.command("purge-spec")
def purge_spec_cmd(
    spec_id: str = typer.Argument(..., help="spec_id"),
    provider: Provider = typer.Option("voyage"),
    skip_pg: bool = typer.Option(False),
) -> None:
    """清掉 Qdrant + BM25 + PG 中该 spec 的所有写入（重建前的清理）。"""
    qdrant = QdrantWriter(provider=provider)
    bm25 = BM25Writer(provider=provider)
    pg: PgChunkMetaWriter | None = None
    if not skip_pg and default_database_url():
        try:
            pg = PgChunkMetaWriter.from_env(provider=provider, schema_owner=False)
        except Exception as exc:
            typer.echo(f"[purge-spec] PG disabled: {exc}")

    qd = qdrant.purge_spec(spec_id)
    bm = bm25.purge_spec(spec_id)
    pgn = pg.purge_spec(spec_id) if pg else 0
    typer.echo(
        f"[purge-spec] spec={spec_id} provider={provider} "
        f"qdrant={qd} bm25={'yes' if bm else 'no'} pg={pgn}"
    )


__all__ = ["app"]
