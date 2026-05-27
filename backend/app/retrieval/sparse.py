"""BM25 Sparse 检索（从 ingestion 端持久化目录加载）。

约定（`02-ingestion-and-indexing.md §4.4 / 03-agent.md §4.5`）：

- ingestion 端把 BM25 检索源持久化到 `{INGEST_DATA_DIR}/bm25/{provider}/by_spec/{spec}.jsonl`，
  并在 `finalize()` / `bm25-rebuild` 时把索引落盘到 `{INGEST_DATA_DIR}/bm25/{provider}/index/`
- backend 启动时优先走**持久化 fast path**：
  `bm25s.BM25.load(index_dir, mmap=True, load_corpus=False)` + `JsonlCorpus(corpus.jsonl)`
  → 索引矩阵走 mmap、corpus 走 byte-offset lazy 读，**RSS 仅占索引矩阵冷数据 + offset 数组**
- 找不到 `index/` 时 fallback 走旧 in-memory 路径（一次性把 jsonl 反序列化进 `list[dict]`
  再现场 build），并打 warning：394k chunks 全量 list[dict] 实测 ~2.5 GiB VmSize，应避免。

实现：

- 用 `bm25s` 直接构建（不经 LlamaIndex BM25Retriever，避免 wrapper 开销）
- 加载是 blocking IO + CPU；fast path < 1s，fallback path ~30-60s。封装为
  `from_directory` classmethod，caller 可在 FastAPI lifespan 里 `asyncio.to_thread(...)` 跑
- 查询接口 sync，因为 bm25s 内部无 IO；caller 若要 async 可用 `asyncio.to_thread`
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

import bm25s  # type: ignore[import-untyped]
from bm25s.utils.corpus import JsonlCorpus  # type: ignore[import-untyped]

from app.core.config import Settings, get_settings
from app.core.errors import RetrievalError

from .models import RetrievedChunk

log = logging.getLogger(__name__)


# 与 ingestion.indexer.bm25_writer._TOKENIZE_STOPWORDS 保持一致
_TOKENIZE_STOPWORDS = "en"


class _RecordStore(Protocol):
    """统一接口：`list[dict]`（fallback）与 `JsonlCorpus`（fast path）都支持。"""

    def __getitem__(self, index: int) -> dict[str, Any]: ...

    def __len__(self) -> int: ...


class SparseRetriever:
    """In-memory BM25 检索器。

    线程安全：bm25s 检索是 read-only。多线程并发查询安全。
    """

    def __init__(
        self,
        *,
        bm25_index: bm25s.BM25,
        records: _RecordStore,
        backend: str = "legacy",
    ) -> None:
        self._bm25 = bm25_index
        self._records = records
        self._backend = backend

    @property
    def backend(self) -> str:
        """`mmap` / `legacy`；供日志和 health 端点区分加载路径。"""
        return self._backend

    # ---------- 构造 ----------

    @classmethod
    def from_directory(
        cls,
        directory: str | Path,
        *,
        spec_ids: Sequence[str] | None = None,
    ) -> SparseRetriever:
        """优先 mmap fast path；找不到 `index/` 时 fallback 到旧 in-memory build。

        `spec_ids` 非空时**强制走 legacy 路径**：fast path 是全量索引，按 spec 过滤需要
        重新 build；spec_ids 只在单测 / POC 小数据集场景使用，重 build 也很快。
        """
        directory = Path(directory)

        if spec_ids is None and _has_persisted_index(directory):
            try:
                return cls._from_persisted_index(directory)
            except Exception as exc:
                log.warning(
                    "bm25: persisted index at %s/index/ unusable (%s); falling back to "
                    "legacy in-memory build (RSS will be high)",
                    directory,
                    exc,
                )

        if spec_ids is None:
            log.warning(
                "bm25: no persisted index at %s/index/; falling back to legacy in-memory build "
                "(394k chunks ≈ 2.5 GiB VmSize). 跑 `ingestion bm25-rebuild` 生成 index/ 后重启",
                directory,
            )
        return cls._from_jsonl_dir(directory, spec_ids=spec_ids)

    @classmethod
    def _from_persisted_index(cls, directory: Path) -> SparseRetriever:
        index_dir = directory / "index"
        corpus_path = index_dir / "corpus.jsonl"
        if not corpus_path.exists():
            raise RetrievalError(f"bm25 persisted index missing corpus.jsonl at {corpus_path}")

        bm25_index = bm25s.BM25.load(str(index_dir), mmap=True, load_corpus=False)
        corpus = JsonlCorpus(str(corpus_path), show_progress=False)
        log.info(
            "bm25 loaded from persisted index (mmap): docs=%d dir=%s",
            len(corpus),
            index_dir,
        )
        return cls(bm25_index=bm25_index, records=corpus, backend="mmap")

    @classmethod
    def _from_jsonl_dir(
        cls,
        directory: Path,
        *,
        spec_ids: Sequence[str] | None = None,
    ) -> SparseRetriever:
        records = list(_iter_jsonl(directory, spec_ids=spec_ids))
        if not records:
            raise RetrievalError(f"no bm25 records found under {directory}")
        return cls.from_records(records)

    @classmethod
    def from_records(cls, records: Sequence[dict[str, Any]]) -> SparseRetriever:
        rec_list = list(records)
        if not rec_list:
            raise RetrievalError("from_records called with empty records")
        corpus = [r.get("content") or "" for r in rec_list]
        tokens = bm25s.tokenize(corpus, stopwords=_TOKENIZE_STOPWORDS)
        bm25 = bm25s.BM25()
        bm25.index(tokens, show_progress=False)
        log.info("bm25 indexed (legacy in-memory build): docs=%d", len(rec_list))
        return cls(bm25_index=bm25, records=rec_list, backend="legacy")

    @classmethod
    def from_env(cls, *, settings: Settings | None = None) -> SparseRetriever:
        s = settings or get_settings()
        return cls.from_directory(s.bm25_dir)

    # ---------- 查询 ----------

    def retrieve(self, query: str, *, top_k: int = 30) -> list[RetrievedChunk]:
        if not query.strip():
            return []
        qtokens = bm25s.tokenize([query], stopwords=_TOKENIZE_STOPWORDS)
        k = min(top_k, len(self._records))
        results, scores = self._bm25.retrieve(qtokens, k=k, show_progress=False)
        chunks: list[RetrievedChunk] = []
        for doc_id, score in zip(results[0].tolist(), scores[0].tolist(), strict=True):
            rec = self._records[int(doc_id)]
            chunks.append(_record_to_chunk(rec, score=float(score)))
        return chunks

    @property
    def n(self) -> int:
        return len(self._records)


def _has_persisted_index(directory: Path) -> bool:
    """与 `BM25Writer.has_persisted_index` 等价的探测逻辑（不引入 ingestion 依赖）。"""
    return (directory / "index" / "params.index.json").exists()


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
