"""一次性脚本：从 38331_chunks.jsonl 读取 chunks，dedupe 后只 upsert PG。

理由：Voyage embedding 已经在 Qdrant 中持久化（8853 unique points），
BM25 by_spec 文件也已写入。前一次 indexer 跑被 PG UNIQUE 卡住失败
（chunker 重复 chunk_id bug），重新跑 indexer 会浪费 ~$0.5 重新 embedding。
本脚本直接 upsert PG 完成 POC。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime

from ingestion.chunker.models import Chunk
from ingestion.indexer.pg_writer import PgChunkMetaWriter

logging.basicConfig(level="INFO")
log = logging.getLogger("poc_fix_pg_only")


def load_chunks_from_jsonl(path: str) -> list[Chunk]:
    out: list[Chunk] = []
    seen_ids: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            cid = d["chunk_id"]
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            d["section_path"] = tuple(d["section_path"])
            d["created_at"] = datetime.fromisoformat(d["created_at"])
            out.append(Chunk(**d))
    return out


def main() -> int:
    jsonl_path = sys.argv[1] if len(sys.argv) > 1 else "/data/tgpp/poc/38331_chunks.jsonl"
    provider = os.environ.get("INDEX_PROVIDER", "voyage")
    chunks = load_chunks_from_jsonl(jsonl_path)
    log.info("loaded %d unique chunks from %s", len(chunks), jsonl_path)

    pg = PgChunkMetaWriter.from_env(provider=provider)
    try:
        # 先 purge 该 spec 的旧记录（防上次失败的部分插入残留）
        spec_ids = sorted({c.spec_id for c in chunks})
        for spec_id in spec_ids:
            removed = pg.purge_spec(spec_id)
            log.info("purged %d rows for spec=%s", removed, spec_id)
        n = pg.upsert_chunks(chunks)
        log.info("upserted %d rows", n)
        for spec_id in spec_ids:
            log.info("pg count for %s: %d", spec_id, pg.count(spec_id=spec_id))
    finally:
        pg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
