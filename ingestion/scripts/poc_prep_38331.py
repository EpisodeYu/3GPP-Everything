"""POC 准备脚本：chunker（含 vision_resolver）跑 38.331，落 JSONL。

用途：在 Voyage payment method 生效前，先把 chunker + 64 张 vision call 跑完：
- vision 描述写入 Redis 缓存（后续 indexer 跑时命中）
- 全量 chunks 落 JSONL 供静态抽检 table / formula / figure 质量

不调 Voyage / GLM embedding，不写 Qdrant / BM25 / PG。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from ingestion.chunker import ChunkParams, build_chunks
from ingestion.hf_loader import (
    GsmaHfLoader,
    dedupe_keep_latest,
    get_meta,
    manifest_session,
    read_entries,
)
from ingestion.images import build_resolver_from_env

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("poc_prep")


def _chunk_to_json(c: object) -> dict:
    d = asdict(c)  # type: ignore[arg-type]
    d["section_path"] = list(d["section_path"])
    d["created_at"] = c.created_at.isoformat()  # type: ignore[attr-defined]
    return d


def main(out_path: str = "/data/tgpp/poc/38331_chunks.jsonl") -> int:
    manifest_path = Path("/data/tgpp/markdown/gsma_manifest.sqlite")
    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")
    es = [e for e in entries if e.spec_id == "38.331"]
    entry = dedupe_keep_latest(es)[0]
    log.info(
        "[poc-prep] spec=%s release=%s images=%d raw_md=%.1fKiB",
        entry.spec_id,
        entry.release,
        entry.image_count,
        entry.raw_md_size / 1024,
    )

    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)
    try:
        vision = build_resolver_from_env()
        log.info("[poc-prep] vision resolver: ON (model=%s)", vision._model)
    except Exception as exc:
        log.error("[poc-prep] vision build failed: %s", exc)
        return 1

    t0 = time.time()
    bundle = next(loader.iter_specs([entry]))
    log.info(
        "[poc-prep] HF load done in %.1fs, starting chunker (with vision)...", time.time() - t0
    )

    t1 = time.time()
    chunks, stats = build_chunks(
        bundle,
        params=ChunkParams(),
        vision_resolver=vision,
    )
    elapsed = time.time() - t1
    log.info("[poc-prep] chunker done in %.1fs", elapsed)
    log.info(
        "[poc-prep] chunks=%d sections kept/dropped/merged=%d/%d/%d",
        stats.chunks_total,
        stats.sections_kept,
        stats.sections_dropped,
        stats.sections_merged,
    )
    log.info("[poc-prep] chunks_by_type=%s", dict(stats.chunks_by_type))
    log.info(
        "[poc-prep] figure_count=%d figure_with_vision=%d",
        stats.figure_count,
        stats.figure_with_vision,
    )

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(_chunk_to_json(c), ensure_ascii=False) + "\n")
    log.info("[poc-prep] wrote %d chunks → %s", len(chunks), out)

    import contextlib

    with contextlib.suppress(Exception):
        vision.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/data/tgpp/poc/38331_chunks.jsonl"))
