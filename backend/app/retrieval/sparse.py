"""BM25 Sparse 检索（从 ingestion 端持久化目录加载）。

约定（`02-ingestion-and-indexing.md §4.4 / 03-agent.md §4.5`）：

- ingestion 端把 BM25 检索源持久化到 `{INGEST_DATA_DIR}/bm25/{provider}/by_spec/{spec}.jsonl`
- backend 端启动时一次性加载所有 jsonl → 构建 in-memory bm25s 索引
- 全量数据规模：394k chunks × 平均几百字符；bm25s build 实测 ~30-60s，可接受

实现：
- 用 `bm25s` 直接构建（不经 LlamaIndex BM25Retriever，避免 wrapper 开销）
- 加载 + 构建 = blocking IO + CPU，封装为 `from_directory` classmethod；
  caller 可在 FastAPI lifespan 里 `asyncio.to_thread(...)` 跑，**不**写在 hot path
- 查询接口 sync，因为 bm25s 内部无 IO；caller 若要 async 可用 `asyncio.to_thread`
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import bm25s

from app.core.config import Settings, get_settings
from app.core.errors import RetrievalError

from .models import RetrievedChunk

log = logging.getLogger(__name__)


class SparseRetriever:
    """In-memory BM25 检索器。

    线程安全：bm25s 检索是 read-only。多线程并发查询安全。
    """

    def __init__(
        self,
        *,
        bm25_index: bm25s.BM25,
        records: list[dict[str, Any]],
    ) -> None:
        self._bm25 = bm25_index
        self._records = records

    # ---------- 构造 ----------

    @classmethod
    def from_directory(
        cls,
        directory: str | Path,
        *,
        spec_ids: Sequence[str] | None = None,
    ) -> SparseRetriever:
        """从 `by_spec/*.jsonl` 加载 + 构建 BM25。

        `spec_ids` 非空时只加载指定子集（M4.0 smoke test / 单测用）。
        """
        records = list(_iter_jsonl(directory, spec_ids=spec_ids))
        if not records:
            raise RetrievalError(f"no bm25 records found under {directory}")
        return cls.from_records(records)

    @classmethod
    def from_records(cls, records: Iterable[dict[str, Any]]) -> SparseRetriever:
        rec_list = list(records)
        if not rec_list:
            raise RetrievalError("from_records called with empty records")
        corpus = [r.get("content") or "" for r in rec_list]
        tokens = bm25s.tokenize(corpus, stopwords="en")
        bm25 = bm25s.BM25()
        bm25.index(tokens, show_progress=False)
        log.info("bm25 indexed: docs=%d", len(rec_list))
        return cls(bm25_index=bm25, records=rec_list)

    @classmethod
    def from_env(cls, *, settings: Settings | None = None) -> SparseRetriever:
        s = settings or get_settings()
        return cls.from_directory(s.bm25_dir)

    # ---------- 查询 ----------

    def retrieve(self, query: str, *, top_k: int = 30) -> list[RetrievedChunk]:
        if not query.strip():
            return []
        qtokens = bm25s.tokenize([query], stopwords="en")
        k = min(top_k, len(self._records))
        results, scores = self._bm25.retrieve(qtokens, k=k, show_progress=False)
        # results shape: (1, k) of doc ids; scores shape: (1, k)
        chunks: list[RetrievedChunk] = []
        for doc_id, score in zip(results[0].tolist(), scores[0].tolist(), strict=True):
            rec = self._records[int(doc_id)]
            chunks.append(_record_to_chunk(rec, score=float(score)))
        return chunks

    @property
    def n(self) -> int:
        return len(self._records)


def _iter_jsonl(
    directory: str | Path,
    *,
    spec_ids: Sequence[str] | None = None,
) -> Iterator[dict[str, Any]]:
    d = Path(directory) / "by_spec"
    if not d.is_dir():
        raise RetrievalError(f"bm25 by_spec dir not found: {d}")

    if spec_ids is None:
        files = sorted(d.glob("*.jsonl"))
    else:
        files = [d / f"{sid}.jsonl" for sid in spec_ids]
        missing = [f for f in files if not f.exists()]
        if missing:
            raise RetrievalError(f"bm25 file missing for specs: {missing}")

    for path in files:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    log.warning("bm25 jsonl malformed line at %s", path)


def _record_to_chunk(rec: dict[str, Any], *, score: float) -> RetrievedChunk:
    section_path = rec.get("section_path") or rec.get("clause") or ""
    if isinstance(section_path, str):
        section_path_tuple = tuple(p for p in section_path.split(".") if p)
    else:
        section_path_tuple = tuple(str(p) for p in section_path)
    return RetrievedChunk(
        chunk_id=str(rec.get("chunk_id") or ""),
        spec_id=str(rec.get("spec_id") or ""),
        section_path=section_path_tuple,
        section_title=str(rec.get("section_title") or ""),
        chunk_type=str(rec.get("chunk_type") or "text"),
        content=str(rec.get("content") or ""),
        score_sparse=score,
        extra={
            k: v
            for k, v in rec.items()
            if k
            not in {
                "chunk_id",
                "spec_id",
                "section_path",
                "section_title",
                "chunk_type",
                "content",
                "clause",
            }
        },
    )
