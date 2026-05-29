"""检查 38.413 是否也是 garbage_filter TOC bug 误杀。

任选几个被报告"缺段"的 clause，看 markdown_parser 是否识别 + 是否被 filter 干掉。
如果 parser 没识别 → 不是 chunker bug，是 markdown 源 / parser 问题。
如果 filter 干掉 → 同样 TOC bug。
如果都过了 → merger 合并行为（设计如此）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ingestion.chunker.garbage_filter import is_garbage
from ingestion.hf_loader import (
    GsmaHfLoader,
    dedupe_keep_latest,
    get_meta,
    manifest_session,
    read_entries,
)
from ingestion.hf_loader.markdown_parser import parse_markdown_sections


def _manifest() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


# 从 audit 报告里挑几个有代表性的 "缺段"
WATCH_38413 = {
    "8.3.3.3",  # Abnormal Conditions pattern
    "9.2.2.7",  # 单点缺
    "9.3.1.35",  # 大段连续缺起点
    "9.3.1.36",
    "9.3.1.85",  # 单点缺
    "10.3",  # 顶层 chapter 缺
}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    manifest = _manifest()
    with manifest_session(manifest) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")
    entry = dedupe_keep_latest([e for e in entries if e.spec_id == "38.413"])[0]
    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)
    bundle = next(loader.iter_specs([entry]))
    sections = parse_markdown_sections(bundle.raw_markdown, spec_id="38.413", release=entry.release)

    print(f"38.413 parser produced {len(sections)} sections")
    by_clause = {s.clause: s for s in sections}
    print("\n=== 检查 WATCH clauses ===")
    for clause in sorted(WATCH_38413):
        if clause not in by_clause:
            print(f"  ❌ clause={clause!r} parser 没识别到 → markdown 源 / parser 问题")
            continue
        sec = by_clause[clause]
        is_drop, reason = is_garbage(sec)
        marker = "❌ DROPPED" if is_drop else "✓ kept"
        print(
            f"  {marker} clause={clause!r} title={sec.section_title!r} "
            f"chars={sec.body_chars} reason={reason!r}"
        )


if __name__ == "__main__":
    main()
