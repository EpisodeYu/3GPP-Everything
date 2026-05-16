"""BM25 持久化层。

设计选择（docs §4.4 / §4.7）：
**ingestion 端只持久化"BM25 检索源"（chunk_id + content + 关键 metadata），
不构建 BM25 矩阵；backend 加载时由 LlamaIndex `BM25Retriever` 现场构建。**

理由：

1. backend 已有 `llama-index-retrievers-bm25` 依赖；ingestion 加 llama-index 太重
2. 50k+ chunks 现场 build < 60s（文档已确认）；backend 启动时一次 build 完全可接受
3. 持久化文件简单 + 通用：`chunks.jsonl` 任何 BM25 实现都能用
4. 升级 BM25 算法 / 切 tokenizer 不需要重跑 ingestion

文件结构：

```
{INGEST_DATA_DIR}/bm25/{provider}/
├── chunks.jsonl       # 每行 {"chunk_id","spec_id","clause","content",...}
├── meta.json          # {provider,total_chunks,written_at,manifest}
└── by_spec/
    └── {spec_id}.jsonl   # 按 spec 拆分（spec 级 purge / 重建用）
```

写入策略：

- **spec 级文件**（`by_spec/{spec_id}.jsonl`）：单 spec 重跑时覆盖，幂等
- **合并文件**（`chunks.jsonl`）：在 pipeline 结束阶段从 `by_spec/` 重新拼出
- meta.json：每次写都更新，含 timestamp 与每 spec 的 chunk 数

调用：

```python
writer = BM25Writer(provider="voyage", base_dir=...)
writer.write_spec_chunks("38.331", chunks)
writer.finalize()                # 把 by_spec/ 合成 chunks.jsonl + 更新 meta.json
```
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


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

    def finalize(self) -> dict[str, Any]:
        """把 by_spec/*.jsonl 合并为 chunks.jsonl + 更新 meta.json。

        生产建议在 `pipeline-hf` 跑完所有 spec 后调一次；增量场景也可调。

        幂等语义（多次执行）：
        - `chunks.jsonl` 走 atomic `tmp.replace(target)` **truncate 重写**，
          重复 finalize 不会 append 出 duplicate 行（M2 17 篇 POC §3.6 实测确认：
          38.300 失败 → 重跑后再 finalize，行数仍 = sum(by_spec/*.jsonl)，无重复）
        - `meta.json` 整体 overwrite；written_at 反映最后一次执行时间
        - 任何在 `by_spec/` 之外的旧 `chunks.jsonl` 行会被清理：source-of-truth
          只看 `by_spec/*.jsonl`
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

        meta = {
            "provider": self.provider,
            "total_chunks": total,
            "spec_count": len(per_spec_counts),
            "by_spec": per_spec_counts,
            "written_at": int(time.time()),
            "chunks_file": str(self.chunks_file),
        }
        self.meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        log.info(
            "bm25: finalized provider=%s total=%d specs=%d → %s",
            self.provider,
            total,
            len(per_spec_counts),
            self.chunks_file,
        )
        return meta

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
        if not self.chunks_file.exists():
            return
        with self.chunks_file.open(encoding="utf-8") as f:
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
