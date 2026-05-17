"""glossary 抽取 CLI 子命令。

被 ingestion/cli.py 通过 ``app.registered_commands`` 挂在顶层，注册名 ``glossary-extract``。

设计要点：
- 默认从 ``$INGEST_DATA_DIR/markdown/gsma_manifest.sqlite`` 读 manifest，避免重复扫 HF tree
  （manifest 由 ``ingestion hf-pull`` 维护）。
- ``--all``：跨 release 同 spec_id 保留最新，按 TS 5G 系列白名单过滤，**额外**保留 21.905 TR
  （docs/03-development/03-agent.md §0 M4.1 明确从 21.905 + 各 TS Definitions 章节抽）。
- ``--spec-ids``：手工指定一批 spec_id，跳过白名单。
- ``--limit``：debug 用，仅处理前 N 篇。
- ``--dry-run``：只解析不写 PG。
- 错误隔离：单 spec 抽取 / 写入失败仅记 warn，继续下一篇；最后统计 succeeded/failed。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import typer

from ..hf_loader.loader import GsmaHfLoader
from ..hf_loader.manifest_store import manifest_session, read_entries
from ..hf_loader.models import SpecManifestEntry
from ..hf_loader.spec_grouper import (
    TS_5G_SERIES_WHITELIST,
    dedupe_keep_latest,
    filter_ts_5g,
)
from .extractor import extract_from_sections
from .writer import PgGlossaryWriter

log = logging.getLogger(__name__)

# 21.905 是 "Vocabulary for 3GPP Specifications" TR；不在 TS 白名单内但是 glossary 主源。
_ALWAYS_INCLUDE_SPEC_IDS: frozenset[str] = frozenset({"21.905"})

app = typer.Typer(no_args_is_help=True, help="Glossary 抽取（M4.1）")


def _default_manifest_path() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


def _hf_token() -> str | None:
    return os.environ.get("HF_TOKEN") or None


def _load_manifest_entries(manifest_path: Path) -> list[SpecManifestEntry]:
    if not manifest_path.exists():
        raise typer.BadParameter(
            f"manifest not found: {manifest_path}. 先跑 `ingestion hf-pull`。"
        )
    with manifest_session(manifest_path) as conn:
        return read_entries(conn)


def _select_entries(
    entries: list[SpecManifestEntry],
    *,
    all_specs: bool,
    spec_ids: list[str] | None,
) -> list[SpecManifestEntry]:
    deduped = dedupe_keep_latest(entries)
    if spec_ids:
        wanted = set(spec_ids)
        return [e for e in deduped if e.spec_id in wanted]
    # all_specs：TS 5G 白名单 + 额外固定纳入的 TR（21.905）
    primary = filter_ts_5g(deduped, whitelist=TS_5G_SERIES_WHITELIST)
    primary_ids = {e.spec_id for e in primary}
    extras = [
        e
        for e in deduped
        if e.spec_id in _ALWAYS_INCLUDE_SPEC_IDS and e.spec_id not in primary_ids
    ]
    return primary + extras


@app.command("glossary-extract")
def glossary_extract(
    all_specs: bool = typer.Option(
        False, "--all", help="处理 TS 5G 白名单 + 21.905（M4.1 生产口径）"
    ),
    spec_ids: str | None = typer.Option(
        None, "--spec-ids", help="逗号分隔 spec_id 列表（覆盖 --all 的白名单选择）"
    ),
    limit: int | None = typer.Option(None, "--limit", help="最多处理 N 篇（debug）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只解析不写 PG"),
    database_url: str | None = typer.Option(
        None, "--database-url", help="覆盖 .env DATABASE_URL"
    ),
    manifest: Path | None = typer.Option(
        None, "--manifest", help="SQLite manifest 路径，默认 $INGEST_DATA_DIR/markdown/gsma_manifest.sqlite"
    ),
    revision: str | None = typer.Option(
        None, "--revision", envvar="GSMA_REVISION", help="HF revision，默认取 manifest 中已 pull 的最新"
    ),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """从 ``21.905`` + 各 TS Definitions / Abbreviations 章节抽 term 写 PG glossary。"""
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not all_specs and not spec_ids:
        raise typer.BadParameter("Provide --all or --spec-ids.")

    parsed_spec_ids: list[str] | None = None
    if spec_ids:
        parsed_spec_ids = [s.strip() for s in spec_ids.split(",") if s.strip()]

    manifest_path = manifest or _default_manifest_path()
    entries = _load_manifest_entries(manifest_path)
    selected = _select_entries(entries, all_specs=all_specs, spec_ids=parsed_spec_ids)
    if limit is not None:
        selected = selected[:limit]

    typer.echo(f"[glossary-extract] manifest={manifest_path} selected={len(selected)} specs")

    loader = GsmaHfLoader(revision=revision, token=_hf_token())
    typer.echo(f"[glossary-extract] HF revision={loader.revision}")

    writer: PgGlossaryWriter | None = None
    if not dry_run:
        writer = PgGlossaryWriter.from_env(database_url=database_url, schema_owner=False)

    succeeded = 0
    failed: list[tuple[str, str]] = []
    total_rows = 0
    t0 = time.time()
    try:
        for bundle in loader.iter_specs(selected, parse_sections=True):
            spec_id = bundle.entry.spec_id
            try:
                glossary_entries = extract_from_sections(
                    bundle.sections,
                    spec_id=spec_id,
                    source_revision=bundle.dataset_revision,
                )
                if writer is None:
                    typer.echo(
                        f"[glossary-extract] [DRY] spec={spec_id} extracted={len(glossary_entries)}"
                    )
                else:
                    n = writer.upsert_spec(spec_id, glossary_entries)
                    total_rows += n
                    typer.echo(
                        f"[glossary-extract] spec={spec_id} extracted={len(glossary_entries)} upserted={n}"
                    )
                succeeded += 1
            except Exception as exc:
                log.exception("Failed to extract glossary from %s", spec_id)
                failed.append((spec_id, repr(exc)))
    finally:
        if writer is not None:
            writer.close()

    elapsed = time.time() - t0
    typer.echo(
        f"[glossary-extract] done. specs_succeeded={succeeded} specs_failed={len(failed)} "
        f"rows_upserted={total_rows} elapsed={elapsed:.1f}s"
    )
    if failed:
        for spec_id, msg in failed[:10]:
            typer.echo(f"  - {spec_id}: {msg}")
