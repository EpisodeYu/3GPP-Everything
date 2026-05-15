"""ingestion/chunker - 把 SpecBundle 切成 Chunk 列表。

详见 docs/03-development/02-ingestion-and-indexing.md §4.3 与 plan §3。

公共入口：

- `Chunk`                  最终 chunk 数据契约
- `AtomicBlock`            section body 的原子化中间结构
- `build_chunks`           主入口：SpecBundle → list[Chunk]
- `count_tokens`           Voyage tokenizer token 计数（其他模块共享）
"""

from .builder import (
    DEFAULT_TOKENIZER_MODEL,
    BuildStats,
    ChunkParams,
    build_chunks,
)
from .models import AtomicBlock, AtomicKind, Chunk, ChunkType
from .tokenize_utils import (
    count_tokens,
    count_tokens_batch,
    split_by_tokens,
    tokenize,
    truncate_to_tokens,
)

__all__ = [
    "DEFAULT_TOKENIZER_MODEL",
    "AtomicBlock",
    "AtomicKind",
    "BuildStats",
    "Chunk",
    "ChunkParams",
    "ChunkType",
    "build_chunks",
    "count_tokens",
    "count_tokens_batch",
    "split_by_tokens",
    "tokenize",
    "truncate_to_tokens",
]
