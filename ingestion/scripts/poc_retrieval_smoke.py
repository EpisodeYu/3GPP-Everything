"""POC retrieval smoke: 在 Qdrant tgpp_chunks_voyage 上跑几个 hard query,看 top-5 是否合理。

Hard queries 设计原则:
1. 测概念覆盖（RRC state transitions）
2. 测精确术语（RACH 4-step / 2-step）
3. 测公式（measurement filtering coefficient）
4. 测 ASN1 element（ChannelAccessConfig）
5. 测 figure 描述（mobility procedure handover）
6. 测跨章节（SIB1 scheduling）
"""

from __future__ import annotations

import json
import os
import sys

from ingestion.indexer.embedder import Embedder
from ingestion.indexer.qdrant_writer import QdrantWriter

QUERIES = [
    "RRC state machine transitions between RRC_CONNECTED RRC_IDLE RRC_INACTIVE",
    "Which procedure is used when UE moves from RRC_INACTIVE to RRC_CONNECTED?",
    "What is the formula for L3 filtering of measurement results?",
    "RACH 4-step random access procedure messages msg1 msg2 msg3 msg4",
    "How is SIB1 acquired and what does it schedule?",
    "ChannelAccessConfig IE structure for shared spectrum access",
    "Event A3 entering and leaving conditions threshold offset hysteresis",
    "SecurityModeCommand and SecurityModeComplete messages",
]


def main() -> int:
    provider = "voyage"
    emb = Embedder.from_env(provider=provider)
    q = QdrantWriter(provider=provider)
    print(f"collection: {q.collection_name}, total points: {q.count()}\n")
    results: list[dict] = []
    for query in QUERIES:
        try:
            vec = emb.embed_texts([query]).vectors[0]
        except Exception as e:
            print(f"[FAIL embedding] {query}: {e}")
            continue
        hits = q._client.query_points(
            collection_name=q.collection_name,
            query=vec,
            limit=5,
            with_payload=True,
        ).points
        print(f"=== Q: {query!r}")
        per_q = {"query": query, "hits": []}
        for i, h in enumerate(hits, 1):
            p = h.payload or {}
            content_head = (p.get("content") or "")[:150].replace("\n", " ")
            print(
                f"  #{i} score={h.score:.3f} clause={p.get('clause') or '-':<10} type={p.get('chunk_type')}"
            )
            print(f"       title: {(p.get('section_title') or '')[:80]}")
            print(f"       head:  {content_head}")
            per_q["hits"].append(
                {
                    "rank": i,
                    "score": h.score,
                    "chunk_id": h.id,
                    "clause": p.get("clause"),
                    "chunk_type": p.get("chunk_type"),
                    "section_title": p.get("section_title"),
                    "content_head": content_head,
                }
            )
        print()
        results.append(per_q)
    out = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "/home/s1yu/3GPP-Everything/eval-results/poc-38331/38331_retrieval_smoke.json"
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"wrote {out}")
    emb.close()
    q.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
