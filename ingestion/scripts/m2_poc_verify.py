"""m2-poc 17 篇 POC 索引一致性校验 + cost / 速率统计。

校验逻辑（按 §8.6 "两 collection point 数 == chunker 输出 == BM25 行数 == PG 行数"）：
  for spec in spec_ids:
    - qdrant `_d2048` count(spec_id) == qdrant `_d1024` count(spec_id)
    - PG chunks_meta count(spec_id) == 上
    - BM25 by_spec/{spec_id}.jsonl 行数 == 上
    - 跨 dim chunk_id 集合相等
  汇总：
    - 总 chunks / per spec chunks_total
    - voyage embedding tokens（从 PipelineStats 取）
    - voyage cost 估算（按 $0.12/M tokens）
    - 端到端耗时 / 单 spec 平均

输出：eval-results/m2-poc/17specs_verify.json + 17specs_throughput.md
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import psycopg

SPEC_IDS_TASK_D = [
    "23.401",
    "23.501",
    "23.502",
    "23.503",
    "24.501",
    "29.500",
    "29.501",
    "29.502",
    "29.503",
    "29.518",
    "36.213",
    "38.214",
    "38.300",
    "38.401",
    "38.413",
    "38.473",
]
SPEC_IDS_ALL_POC = ["38.331", *SPEC_IDS_TASK_D]  # 包含 M1 POC 已索引的 38.331

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION_PREFIX = os.environ.get("QDRANT_COLLECTION_PREFIX", "tgpp_chunks")
PROVIDER = "voyage"
DIMS = [2048, 1024]
BM25_DIR = Path(os.environ.get("INGEST_DATA_DIR", "/data/tgpp")) / "bm25" / PROVIDER
# DB 连接串从环境读取（psycopg 格式，不含 +asyncpg）；绝不在代码里写死密码。
DATABASE_URL = os.environ.get("DATABASE_URL_RAW")
if not DATABASE_URL:
    raise SystemExit(
        "DATABASE_URL_RAW 未设置；例：postgresql://tgpp_app:<password>@localhost:5432/tgpp_everything"
    )


def qdrant_count_by_spec(client: httpx.Client, collection: str, spec_id: str) -> int:
    r = client.post(
        f"{QDRANT_URL}/collections/{collection}/points/count",
        json={
            "exact": True,
            "filter": {"must": [{"key": "spec_id", "match": {"value": spec_id}}]},
        },
    )
    r.raise_for_status()
    return int(r.json()["result"]["count"])


def qdrant_chunk_ids(client: httpx.Client, collection: str, spec_id: str) -> set[str]:
    ids: set[str] = set()
    offset = None
    while True:
        body: dict = {
            "limit": 1000,
            "with_payload": ["chunk_id"],
            "with_vector": False,
            "filter": {"must": [{"key": "spec_id", "match": {"value": spec_id}}]},
        }
        if offset is not None:
            body["offset"] = offset
        r = client.post(f"{QDRANT_URL}/collections/{collection}/points/scroll", json=body)
        r.raise_for_status()
        res = r.json()["result"]
        for p in res["points"]:
            cid = p["payload"].get("chunk_id") if p.get("payload") else None
            if cid is None:
                cid = str(p.get("id"))
            ids.add(cid)
        offset = res.get("next_page_offset")
        if not offset:
            break
    return ids


def pg_count_by_spec(spec_id: str) -> int:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM chunks_meta WHERE spec_id = %s AND provider = %s",
            (spec_id, PROVIDER),
        )
        return int(cur.fetchone()[0])


def bm25_count_by_spec(spec_id: str) -> int:
    p = BM25_DIR / "by_spec" / f"{spec_id}.jsonl"
    if not p.exists():
        return 0
    with p.open() as f:
        return sum(1 for _ in f)


def verify_spec(client: httpx.Client, spec_id: str) -> dict:
    counts: dict[str, int] = {}
    for d in DIMS:
        col = f"{COLLECTION_PREFIX}_{PROVIDER}_d{d}"
        counts[f"qdrant_d{d}"] = qdrant_count_by_spec(client, col, spec_id)
    counts["pg_chunks_meta"] = pg_count_by_spec(spec_id)
    counts["bm25_by_spec"] = bm25_count_by_spec(spec_id)
    ids_2048 = qdrant_chunk_ids(client, f"{COLLECTION_PREFIX}_{PROVIDER}_d2048", spec_id)
    ids_1024 = qdrant_chunk_ids(client, f"{COLLECTION_PREFIX}_{PROVIDER}_d1024", spec_id)
    cross_dim_equal = ids_2048 == ids_1024
    all_counts_equal = len(set(counts.values())) == 1
    return {
        "spec_id": spec_id,
        "counts": counts,
        "cross_dim_chunk_id_equal": cross_dim_equal,
        "all_sink_counts_equal": all_counts_equal,
        "n_2048_only": len(ids_2048 - ids_1024),
        "n_1024_only": len(ids_1024 - ids_2048),
    }


def load_pipeline_stats() -> dict:
    p = Path("/home/s1yu/3GPP-Everything/eval-results/m2-poc/16specs_index_stats.json")
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def main() -> int:
    only_task_d = "--task-d" in sys.argv
    specs = SPEC_IDS_TASK_D if only_task_d else SPEC_IDS_ALL_POC

    client = httpx.Client(timeout=60.0)
    results = []
    print(f"=== Verify {len(specs)} specs ===")
    for s in specs:
        r = verify_spec(client, s)
        results.append(r)
        emoji = "OK" if r["all_sink_counts_equal"] and r["cross_dim_chunk_id_equal"] else "MISMATCH"
        print(
            f"  [{emoji}] {s}: 2048={r['counts']['qdrant_d2048']:>6} "
            f"1024={r['counts']['qdrant_d1024']:>6} "
            f"pg={r['counts']['pg_chunks_meta']:>6} "
            f"bm25={r['counts']['bm25_by_spec']:>6} "
            f"crossdim_eq={r['cross_dim_chunk_id_equal']}"
        )

    total_chunks = sum(r["counts"]["qdrant_d2048"] for r in results)
    all_ok = all(r["all_sink_counts_equal"] and r["cross_dim_chunk_id_equal"] for r in results)

    pipeline = load_pipeline_stats()
    voyage_tokens = pipeline.get("embedding_tokens", 0)
    elapsed = pipeline.get("elapsed_s", 0.0)
    specs_attempted = pipeline.get("specs_attempted", 0)
    specs_succeeded = pipeline.get("specs_succeeded", 0)
    specs_failed = pipeline.get("specs_failed", 0)
    failures = pipeline.get("failures", [])
    qdrant_upserted_by_dim = pipeline.get("qdrant_upserted_by_dim", {})
    mimo_requests = pipeline.get("mimo_requests_total", 0)
    chunks_total_pipeline = pipeline.get("chunks_total", 0)

    summary = {
        "task": "m2-poc 17 specs verify (16 Task D + 38.331)",
        "specs_verified": len(specs),
        "all_consistent": all_ok,
        "total_chunks_qdrant_d2048": total_chunks,
        "pipeline_stats": {
            "specs_attempted": specs_attempted,
            "specs_succeeded": specs_succeeded,
            "specs_failed": specs_failed,
            "failures": failures,
            "chunks_total": chunks_total_pipeline,
            "qdrant_upserted_by_dim": qdrant_upserted_by_dim,
            "embedding_tokens": voyage_tokens,
            "elapsed_s": elapsed,
            "mimo_requests_total": mimo_requests,
        },
        "details": results,
    }

    out_dir = Path("/home/s1yu/3GPP-Everything/eval-results/m2-poc")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "17specs_verify.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nWrote {out_dir / '17specs_verify.json'}")
    print(f"All consistent: {all_ok}")
    print(f"Total chunks (d2048): {total_chunks}")
    if pipeline:
        print(
            f"Task D pipeline: specs {specs_succeeded}/{specs_attempted} succeeded, "
            f"{specs_failed} failed; chunks={chunks_total_pipeline}; "
            f"voyage_tokens={voyage_tokens}; elapsed={elapsed:.1f}s; "
            f"mimo_calls={mimo_requests}"
        )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
