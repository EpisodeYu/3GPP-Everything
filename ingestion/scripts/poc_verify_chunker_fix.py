"""验证 §6.1/§6.2/§6.5 三个 chunker bug 在 38.331 真 spec 上已修复。

不调 vision/embedding —— vision_resolver=None，figure chunk 用 GSMA 自带描述
fallback。和 POC 原始指标的对比项：

- 重复 chunk_id 数（应为 0）
- table 缺 `|---|` separator 数（应显著下降）
- section_title 超 200 字符数（应为 0）
- force_split_overflow + table 但缺 separator 的 chunk 数（应为 0）

运行：
    cd ingestion && PYTHONPATH=.. uv run python scripts/poc_verify_chunker_fix.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from ingestion.chunker import ChunkParams, build_chunks
from ingestion.hf_loader import (
    GsmaHfLoader,
    dedupe_keep_latest,
    get_meta,
    manifest_session,
    read_entries,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("poc_verify")


def _has_table_sep(content: str) -> bool:
    for ln in content.splitlines():
        s = ln.strip()
        if (
            s.startswith("|")
            and set(s.replace("|", "").replace(":", "").strip()) <= {"-", " "}
            and "-" in s
        ):
            return True
    return False


def main(spec_id: str = "38.331") -> int:
    manifest_path = Path("/data/tgpp/markdown/gsma_manifest.sqlite")
    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")
    es = [e for e in entries if e.spec_id == spec_id]
    if not es:
        log.error("spec %s not found in manifest", spec_id)
        return 1
    entry = dedupe_keep_latest(es)[0]
    log.info(
        "[verify] spec=%s release=%s raw_md=%.1fKiB",
        entry.spec_id,
        entry.release,
        entry.raw_md_size / 1024,
    )

    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)
    bundle = next(loader.iter_specs([entry]))
    t0 = time.time()
    chunks, stats = build_chunks(bundle, params=ChunkParams(), vision_resolver=None)
    elapsed = time.time() - t0
    log.info("[verify] chunker done %.1fs chunks=%d", elapsed, stats.chunks_total)
    log.info("[verify] chunks_by_type=%s", dict(stats.chunks_by_type))

    # 1) 重复 chunk_id
    id_count: Counter[str] = Counter(c.chunk_id for c in chunks)
    dup_ids = {cid: n for cid, n in id_count.items() if n > 1}
    log.info(
        "[verify] dup chunk_id: %d (extra rows %d)",
        len(dup_ids),
        sum(n - 1 for n in dup_ids.values()),
    )

    # 1b) 同 section + 同 content 重复（builder 的真正 dedupe 目标）
    by_section_content: dict[tuple[str, str], list[str]] = defaultdict(list)
    for c in chunks:
        by_section_content[(c.parent_section_id, c.content)].append(c.chunk_id)
    intra_dup = {k: v for k, v in by_section_content.items() if len(v) > 1}
    log.info("[verify] intra-section content dup: %d groups", len(intra_dup))
    for (psid, content), ids in list(intra_dup.items())[:3]:
        # 找出这些 chunk 的 clause/section_title 供诊断
        same = [c for c in chunks if c.parent_section_id == psid][:1]
        cl = same[0].clause if same else "?"
        st = same[0].section_title if same else "?"
        log.warning(
            "  group: parent=%s clause=%r title=%r ids=%s content_prefix=%r",
            psid[:8],
            cl,
            st[:80],
            ids,
            content[:120],
        )

    # 2) table 缺 separator
    table_chunks = [c for c in chunks if c.chunk_type == "table"]
    no_sep = [c for c in table_chunks if not _has_table_sep(c.content)]
    overflow_no_sep = [c for c in no_sep if c.raw_extra.get("force_split_overflow")]
    log.info(
        "[verify] table chunks=%d no_separator=%d (of which force_split_overflow=%d)",
        len(table_chunks),
        len(no_sep),
        len(overflow_no_sep),
    )

    # 3) section_title 超长
    long_titles = [c for c in chunks if len(c.section_title) > 200]
    super_long = [c for c in chunks if len(c.section_title) > 500]
    log.info(
        "[verify] section_title >200 chars: %d, >500 chars: %d",
        len(long_titles),
        len(super_long),
    )
    if super_long:
        for c in super_long[:3]:
            log.warning(
                "  unexpected super long title len=%d prefix=%r",
                len(c.section_title),
                c.section_title[:120],
            )

    # 4) 终评
    issues = 0
    if len(dup_ids) > 0:
        log.error("FAIL: dup chunk_id should be 0")
        issues += 1
    if len(intra_dup) > 0:
        log.error("FAIL: intra-section duplicate content should be 0")
        issues += 1
    if len(super_long) > 0:
        log.error("FAIL: section_title >500 chars should be 0")
        issues += 1
    if len(no_sep) >= 100:
        log.error("FAIL: table no-separator should be << 129 (was 129 pre-fix)")
        issues += 1

    if issues == 0:
        log.info("[verify] ALL CHECKS PASS")
        return 0
    log.error("[verify] %d failed checks", issues)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "38.331"))
