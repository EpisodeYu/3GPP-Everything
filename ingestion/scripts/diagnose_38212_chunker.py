"""第二阶段诊断：跑 chunker 看哪一步把 §7.3.1.2.2 / 7.3.1.3.5 / 7.3.1.3.6 吞了。

输出：
- merger 后 section 列表（看是否被合并到其它 clause）
- 每个 section 经 atomic_blocks → splitter 后产 chunk 数
- 最终 chunks 里 §7.3.1.2.2 等关键 clause 的实际出现数
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from pathlib import Path

from ingestion.chunker import ChunkParams, build_chunks
from ingestion.chunker import atomic_blocks as atomic_blocks_mod
from ingestion.chunker.garbage_filter import filter_sections
from ingestion.chunker.merger import merge_short_siblings
from ingestion.chunker.section_splitter import split_section
from ingestion.hf_loader import (
    GsmaHfLoader,
    dedupe_keep_latest,
    get_meta,
    manifest_session,
    read_entries,
)

log = logging.getLogger("diag")


def _manifest() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


WATCH = {"7.3.1.2.2", "7.3.1.1.2", "7.3.1.3.5", "7.3.1.3.6", "5.2", "5.4", "5.3.2"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    manifest = _manifest()
    with manifest_session(manifest) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")
    entry = dedupe_keep_latest([e for e in entries if e.spec_id == "38.212"])[0]

    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)
    bundle = next(loader.iter_specs([entry]))

    print(f"\n=== A) parser → {len(bundle.sections)} sections ===")
    for sec in bundle.sections:
        if sec.clause in WATCH:
            print(
                f"  parser kept clause={sec.clause!r} title={sec.section_title!r} "
                f"chars={sec.body_chars}"
            )

    print("\n=== B) garbage_filter ===")
    kept, dropped, reasons = filter_sections(bundle.sections)
    drop_map = {sec.clause: reasons[sec.clause] for sec in dropped if sec.clause in reasons}
    for clause in sorted(WATCH):
        if any(d.clause == clause for d in dropped):
            print(f"  ❌ DROPPED clause={clause!r} reason={drop_map.get(clause)}")
        elif any(k.clause == clause for k in kept):
            sec = next(k for k in kept if k.clause == clause)
            print(f"  ✓ kept clause={clause!r} chars={sec.body_chars}")
        else:
            print(f"  · clause={clause!r} not in parser output at all")
    print(f"  total kept={len(kept)} dropped={len(dropped)}")

    print("\n=== C) merger ===")
    params = ChunkParams()
    merged_sections = merge_short_siblings(
        kept,
        short_threshold_tokens=params.short_section_threshold,
        target_tokens=params.target_tokens,
        max_tokens=params.max_tokens,
    )
    print(f"  before merger: {len(kept)} → after merger: {len(merged_sections)}")
    for clause in sorted(WATCH):
        matches = [m for m in merged_sections if m.clause == clause]
        if matches:
            sec = matches[0]
            print(f"  ✓ post-merger clause={clause!r} chars={sec.body_chars}")
        else:
            # 看看它是不是被合并到了别的 clause
            holders = [
                m
                for m in merged_sections
                if m.body
                and (f"##### {clause}" in m.body or clause in (m.body[:200] if m.body else ""))
            ]
            if holders:
                for h in holders[:1]:
                    print(
                        f"  ⚠ post-merger clause={clause!r} 被合并到 "
                        f"clause={h.clause!r} title={h.section_title!r} "
                        f"(总 chars={h.body_chars})"
                    )
            else:
                print(f"  ❌ post-merger clause={clause!r} 不见了，且未在其它 section body 里搜到")

    print("\n=== D) atomic_blocks + splitter on watched sections ===")
    for sec in merged_sections:
        if sec.clause not in WATCH:
            continue
        blocks = atomic_blocks_mod.parse_atomic_blocks(sec.body)
        kinds = Counter(b.kind for b in blocks)
        pieces = split_section(
            blocks,
            target_tokens=params.target_tokens,
            max_tokens=params.max_tokens,
            overlap_tokens=params.overlap_tokens,
        )
        piece_kinds = Counter(p.chunk_type for p in pieces)
        print(
            f"  clause={sec.clause!r} body={sec.body_chars} "
            f"blocks={len(blocks)} {dict(kinds)} → pieces={len(pieces)} {dict(piece_kinds)}"
        )

    print("\n=== E) 全量 build_chunks 后 watched clause 实际 chunk 数 ===")
    chunks, stats = build_chunks(bundle, params=params, vision_resolver=None)
    print(
        f"  total chunks={stats.chunks_total} by_type={stats.chunks_by_type} "
        f"sections_total={stats.sections_total} kept={stats.sections_kept} "
        f"dropped={stats.sections_dropped} merged={stats.sections_merged}"
    )
    if stats.drop_reasons:
        print(f"  drop_reasons={stats.drop_reasons}")
    counts = Counter(c.clause for c in chunks)
    for clause in sorted(WATCH):
        n = counts.get(clause, 0)
        marker = "❌" if n == 0 else "✓"
        print(f"  {marker} clause={clause!r} chunks={n}")
    print("\n  top 15 clauses with most chunks:")
    for clause, n in counts.most_common(15):
        print(f"    {clause:<14} {n}")


if __name__ == "__main__":
    main()
