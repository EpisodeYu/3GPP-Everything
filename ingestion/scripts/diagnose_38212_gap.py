"""诊断 38.212 §7.3.1.2.2 (Format 1_1) 缺段根因。

步骤：
1. 从 GSMA HF 拉 38.212 raw.md
2. 在 raw.md 里 grep 全部 `7.3.1.2.X` 出现位置（headings + 引用）
3. 跑 markdown_parser → 输出全部 sections 的 clause/title 列表
4. 报告：源里有没有 "7.3.1.2.2 Format 1_1" 的 heading？parser 是否识别？

read-only / 不写任何下游索引。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ingestion.hf_loader import (
    GsmaHfLoader,
    dedupe_keep_latest,
    get_meta,
    manifest_session,
    read_entries,
)
from ingestion.hf_loader.markdown_parser import parse_markdown_sections

log = logging.getLogger("diagnose_38212")


def _manifest_path() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    manifest = _manifest_path()
    with manifest_session(manifest) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")

    candidates = [e for e in entries if e.spec_id == "38.212"]
    if not candidates:
        raise SystemExit("38.212 not in manifest")
    entry = dedupe_keep_latest(candidates)[0]
    log.info(
        "entry: spec_uid=%s release=%s raw_md=%s", entry.spec_uid, entry.release, entry.raw_md_path
    )

    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)
    bundle = next(loader.iter_specs([entry]))
    raw = bundle.raw_markdown
    log.info("raw_md size = %d bytes", len(raw))

    # 落地 raw md 便于事后翻
    out_raw = Path("/tmp/38212_raw.md")
    out_raw.write_text(raw, encoding="utf-8")
    log.info("raw.md saved to %s", out_raw)

    print("\n=== 1) raw.md 里 '7.3.1.2.X' 出现的所有行 ===")
    pat_clause = re.compile(r"^(.{0,200})7\.3\.1\.2(\.\d+)?\b(.{0,200})$", re.MULTILINE)
    seen = set()
    for m in pat_clause.finditer(raw):
        line = m.group(0).strip()
        if line in seen:
            continue
        seen.add(line)
        if len(seen) > 60:
            print("  ...（截断）")
            break
        # 优先显示像 heading 的（以 # 起头）
        is_heading = line.lstrip().startswith("#")
        marker = "  H>" if is_heading else "  ··"
        print(f"{marker} {line[:240]}")

    print("\n=== 2) raw.md 头部找 'Format 1\\_1' / 'Format 1_1' 等模式 ===")
    pat_fmt = re.compile(r"^.{0,300}Format\s*1[\\_]+1\b.{0,300}$", re.MULTILINE | re.IGNORECASE)
    for idx, m in enumerate(pat_fmt.finditer(raw)):
        line = m.group(0).strip()
        is_heading = line.lstrip().startswith("#")
        marker = "  H>" if is_heading else "  ··"
        print(f"{marker} {line[:240]}")
        if idx >= 20:
            print("  ...（截断）")
            break

    print("\n=== 3) parser 输出全部 7.3.1.x 段 ===")
    sections = parse_markdown_sections(raw, spec_id="38.212", release=entry.release)
    for sec in sections:
        if sec.clause.startswith("7.3.1") or "format 1" in (sec.section_title or "").lower():
            print(
                f"  L{sec.section_level} clause={sec.clause!r:<14} "
                f"title={sec.section_title!r:<40} chars={sec.body_chars}"
            )

    print(f"\nparser produced {len(sections)} sections total")

    print("\n=== 4) 找 7.3.1.2.2 是否在源里有 markdown heading ===")
    pat_heading_722 = re.compile(r"^(#{1,6})\s*7\.3\.1\.2\.2\b.*$", re.MULTILINE)
    hits = list(pat_heading_722.finditer(raw))
    if hits:
        for h in hits:
            print(f"  HIT line={raw[:h.start()].count(chr(10))+1}: {h.group(0)!r}")
    else:
        print("  没找到 ^#+ 7.3.1.2.2 形式的 heading")
        print("  → 尝试其它形态（粗体 / 全字符串 / 不在行首）")
        pat_loose = re.compile(r"7\.3\.1\.2\.2[^\d]")
        for m in list(pat_loose.finditer(raw))[:5]:
            lo = max(0, m.start() - 80)
            hi = min(len(raw), m.end() + 80)
            print(f"  ctx[{m.start()}]: {raw[lo:hi]!r}")


if __name__ == "__main__":
    main()
