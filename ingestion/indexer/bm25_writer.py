"""BM25 持久化层。

设计选择（docs §4.4 / §4.7）：

ingestion 端持久化两类产物：

1. **BM25 检索源**（`chunks.jsonl` + `by_spec/*.jsonl`）：纯文本，spec 级幂等重写
2. **预构建的 BM25 索引**（`index/` 子目录）：`bm25s.BM25.save()` 出来的 CSC 矩阵 +
   vocab + corpus；backend 启动时 `bm25s.BM25.load(mmap=True)`，**不再** 反序列化
   全量 jsonl 到 Python 堆，把 RSS 从 ~1 GiB 降到 ~300 MiB。

历史背景：旧设计是 backend 启动期现场 build。394k chunks 实测 ~30-60s 可接受，
但 `list[dict]` 全量驻留 ~2.5 GiB（VmSize），物理 RAM 仅 4 GiB 的机器会触发严重 swap。
迁移到"ingestion build + backend mmap load"后，build 一次性，backend 内存可控。

文件结构：

```
{INGEST_DATA_DIR}/bm25/{provider}/
├── chunks.jsonl              # 每行 {"chunk_id","spec_id","clause","content",...}
├── meta.json                 # {provider,total_chunks,written_at,index_built_at,...}
├── by_spec/
│   └── {spec_id}.jsonl       # 按 spec 拆分（spec 级 purge / 重建用）
└── index/                    # bm25s.BM25.save() 产物（M8 起）
    ├── data.csc.index.npy
    ├── indices.csc.index.npy
    ├── indptr.csc.index.npy
    ├── vocab.index.json
    ├── params.index.json
    ├── nonoccurrence_array.index.npy
    └── corpus.jsonl          # 与 chunks.jsonl 等价，bm25s 内部约定的存储位置
```

写入策略：

- **spec 级文件**（`by_spec/{spec_id}.jsonl`）：单 spec 重跑时覆盖，幂等
- **合并文件**（`chunks.jsonl`）：在 pipeline 结束阶段从 `by_spec/` 重新拼出
- **`index/`**：在 `finalize()` 末尾基于合并后的 chunks 一次性 tokenize + index + save；
  也可以单独通过 `rebuild_index()` 触发（CLI `bm25-rebuild`），不重跑 chunker/embed
- meta.json：每次写都更新，含 timestamp 与每 spec 的 chunk 数

调用：

```python
writer = BM25Writer(provider="voyage", base_dir=...)
writer.write_spec_chunks("38.331", chunks)
writer.finalize()                # 合并 + build + save index/
# 或单独 rebuild（已有 by_spec/，只想重 build 索引）：
writer.rebuild_index()
```
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import bm25s  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


# tokenize 参数：与 backend.app.retrieval.sparse.SparseRetriever.retrieve 保持一致
# （`bm25s.tokenize(..., stopwords="en")`）；改动需同步两侧
_TOKENIZE_STOPWORDS = "en"


def default_bm25_dir(provider: str, *, base_dir: str | Path | None = None) -> Path:
    """`{INGEST_DATA_DIR}/bm25/{provider}/`。"""
    base = base_dir or os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "bm25" / provider


class BM25Writer:
    """BM25 检索源持久化器。

    线程安全：不安全（按 spec 串行写）。pipeline 默认串行处理 spec。
    """

    def __init__(self, *, provider: str, base_dir: str | Path | None = None) -> None:
        self.provider = provider
        self.root = default_bm25_dir(provider, base_dir=base_dir)
        self.by_spec_dir = self.root / "by_spec"
        self.chunks_file = self.root / "chunks.jsonl"
        self.meta_file = self.root / "meta.json"
        self.index_dir = self.root / "index"

    def _ensure_dirs(self) -> None:
        self.by_spec_dir.mkdir(parents=True, exist_ok=True)

    def write_spec_chunks(self, spec_id: str, chunks: Sequence[Any]) -> int:
        """覆盖写 by_spec/{spec_id}.jsonl。

        spec_id 中的 `/` 不会出现（GSMA 编号是 dotted），但若未来有怪字符可在此防御。
        返回写入条数。
        """
        self._ensure_dirs()
        path = self.by_spec_dir / f"{_safe_filename(spec_id)}.jsonl"
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps(_chunk_to_bm25_record(c), ensure_ascii=False) + "\n")
        tmp.replace(path)  # atomic
        log.info("bm25: wrote %d chunks → %s", len(chunks), path)
        return len(chunks)

    def purge_spec(self, spec_id: str) -> bool:
        path = self.by_spec_dir / f"{_safe_filename(spec_id)}.jsonl"
        if path.exists():
            path.unlink()
            log.info("bm25: purged %s", path)
            return True
        return False

    def finalize(self, *, build_index: bool = True) -> dict[str, Any]:
        """把 by_spec/*.jsonl 合并为 chunks.jsonl + 更新 meta.json + 可选 build 索引。

        生产建议在 `pipeline-hf` 跑完所有 spec 后调一次；增量场景也可调。

        幂等语义（多次执行）：
        - `chunks.jsonl` 走 atomic `tmp.replace(target)` **truncate 重写**，
          重复 finalize 不会 append 出 duplicate 行（M2 17 篇 POC §3.6 实测确认：
          38.300 失败 → 重跑后再 finalize，行数仍 = sum(by_spec/*.jsonl)，无重复）
        - `meta.json` 整体 overwrite；written_at 反映最后一次执行时间
        - 任何在 `by_spec/` 之外的旧 `chunks.jsonl` 行会被清理：source-of-truth
          只看 `by_spec/*.jsonl`
        - `index/` 整体重写（atomic：先写到 `index/.new/` 再 swap），重复 finalize 不残留

        参数：
        - `build_index`：默认 True；False 时只合并 chunks.jsonl + 写 meta.json，
          不触发 BM25 索引构建。测试与"只想刷 chunks.jsonl"场景可关掉。
        """
        self._ensure_dirs()
        spec_files = sorted(self.by_spec_dir.glob("*.jsonl"))
        total = 0
        per_spec_counts: dict[str, int] = {}
        tmp = self.chunks_file.with_suffix(self.chunks_file.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as out:
            for sf in spec_files:
                count = 0
                with sf.open(encoding="utf-8") as inp:
                    for line in inp:
                        if not line.strip():
                            continue
                        out.write(line)
                        count += 1
                per_spec_counts[sf.stem] = count
                total += count
        tmp.replace(self.chunks_file)

        index_info: dict[str, Any] = {}
        if build_index and total > 0:
            index_info = self._build_and_save_index_from_chunks_file()
        elif build_index and total == 0:
            log.info("bm25: skip index build (no chunks)")

        meta = {
            "provider": self.provider,
            "total_chunks": total,
            "spec_count": len(per_spec_counts),
            "by_spec": per_spec_counts,
            "written_at": int(time.time()),
            "chunks_file": str(self.chunks_file),
            **index_info,
        }
        self.meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        log.info(
            "bm25: finalized provider=%s total=%d specs=%d index=%s → %s",
            self.provider,
            total,
            len(per_spec_counts),
            "yes" if index_info else "no",
            self.chunks_file,
        )
        return meta

    def rebuild_index(self) -> dict[str, Any]:
        """只重 build BM25 索引（不重新合并 chunks.jsonl），适合 CLI `bm25-rebuild`。

        前置：`by_spec/*.jsonl` 存在。若 `chunks.jsonl` 缺失/陈旧，会先调
        `finalize(build_index=False)` 把它刷出来再 build。

        返回当前的 meta dict。
        """
        self._ensure_dirs()
        if not self.chunks_file.exists():
            log.info("bm25 rebuild: chunks.jsonl missing, running merge first")
            self.finalize(build_index=False)

        existing_meta = self.read_meta() or {}
        existing_meta["provider"] = self.provider
        if "total_chunks" not in existing_meta or "by_spec" not in existing_meta:
            # 老数据没有 meta；从 by_spec/ 现场统计一次以保留 spec_count / by_spec
            spec_counts: dict[str, int] = {}
            for sf in sorted(self.by_spec_dir.glob("*.jsonl")):
                with sf.open(encoding="utf-8") as inp:
                    spec_counts[sf.stem] = sum(1 for line in inp if line.strip())
            existing_meta["total_chunks"] = sum(spec_counts.values())
            existing_meta["spec_count"] = len(spec_counts)
            existing_meta["by_spec"] = spec_counts
            existing_meta["chunks_file"] = str(self.chunks_file)
            existing_meta.setdefault("written_at", int(time.time()))

        if existing_meta["total_chunks"] == 0:
            log.warning("bm25 rebuild: 0 chunks, skipping")
            self.meta_file.write_text(json.dumps(existing_meta, ensure_ascii=False, indent=2))
            return existing_meta

        index_info = self._build_and_save_index_from_chunks_file()
        existing_meta.update(index_info)
        self.meta_file.write_text(json.dumps(existing_meta, ensure_ascii=False, indent=2))
        log.info(
            "bm25 rebuild: provider=%s total=%d → %s",
            self.provider,
            existing_meta["total_chunks"],
            self.index_dir,
        )
        return existing_meta

    # -------------------- 索引 build/save 内部实现 --------------------

    def _build_and_save_index_from_chunks_file(self) -> dict[str, Any]:
        """读 chunks.jsonl → tokenize → bm25s build → atomic save 到 `index/`。

        atomic 策略：先写到 `index/.new/`，原子 rename 替换旧 `index/`。
        异常时清理 `.new/`，保留旧 `index/` 不破坏。
        """
        records = list(_iter_jsonl_file(self.chunks_file))
        if not records:
            log.info("bm25: empty chunks.jsonl, skipping index build")
            return {}

        corpus_texts = [str(r.get("content") or "") for r in records]
        t0 = time.time()
        tokens = bm25s.tokenize(corpus_texts, stopwords=_TOKENIZE_STOPWORDS, show_progress=False)
        index = bm25s.BM25()
        index.index(tokens, show_progress=False)
        build_elapsed = time.time() - t0

        staging = self.index_dir.with_name(self.index_dir.name + ".new")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        t1 = time.time()
        try:
            index.save(str(staging), corpus=records, show_progress=False)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        save_elapsed = time.time() - t1

        # atomic swap：rename 旧目录到 `.old`，新目录 rename 到目标，删旧
        old = self.index_dir.with_name(self.index_dir.name + ".old")
        if old.exists():
            shutil.rmtree(old)
        if self.index_dir.exists():
            self.index_dir.rename(old)
        staging.rename(self.index_dir)
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)

        log.info(
            "bm25: index built+saved docs=%d build=%.1fs save=%.1fs → %s",
            len(records),
            build_elapsed,
            save_elapsed,
            self.index_dir,
        )
        return {
            "index_dir": str(self.index_dir),
            "index_built_at": int(time.time()),
            "index_doc_count": len(records),
            "bm25s_version": getattr(bm25s, "__version__", "unknown"),
        }

    # -------------------- 只读 / 监控辅助 --------------------

    def list_specs(self) -> list[str]:
        if not self.by_spec_dir.exists():
            return []
        return sorted(p.stem for p in self.by_spec_dir.glob("*.jsonl"))

    def read_meta(self) -> dict[str, Any] | None:
        if not self.meta_file.exists():
            return None
        try:
            return json.loads(self.meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def iter_chunks(self) -> Iterator[dict[str, Any]]:
        """读 chunks.jsonl 流式迭代；backend 加载 BM25 时用。"""
        yield from _iter_jsonl_file(self.chunks_file)

    def has_persisted_index(self) -> bool:
        """是否存在可加载的 bm25s 索引。判定标准：`index/params.index.json` 文件。"""
        return (self.index_dir / "params.index.json").exists()


def _iter_jsonl_file(path: Path) -> Iterator[dict[str, Any]]:
    """通用 jsonl 流式迭代，跳过空行。文件不存在返回空。"""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            yield json.loads(line)


def _chunk_to_bm25_record(c: Any) -> dict[str, Any]:
    """Chunk → BM25 JSONL 记录。

    BM25 检索本身只需 (chunk_id, content)；其他字段是为了 backend 在做命中后能
    一次性拿到展示所需 metadata（避免再查 Qdrant payload）。
    """
    return {
        "chunk_id": c.chunk_id,
        "spec_id": c.spec_id,
        "spec_number": c.spec_number,
        "release": c.release,
        "series": c.series,
        "clause": c.clause,
        "section_title": c.section_title,
        "parent_section_id": c.parent_section_id,
        "chunk_type": c.chunk_type,
        "document_order": c.document_order,
        "content": c.content,
    }


def _safe_filename(name: str) -> str:
    """spec_id 通常是 'NN.MMM' 或 'NN.MMM-K'；统一替换分隔符（防御性）。"""
    return name.replace("/", "_").replace("\\", "_")


__all__ = ["BM25Writer", "default_bm25_dir"]
