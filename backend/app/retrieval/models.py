"""Retrieval 数据契约。

`RetrievedChunk` 是 dense / sparse / rerank / hybrid 之间唯一的交换格式；
agent 端的 `agent.state.RetrievedChunk` 之后从此基础上 mirror（M4.2 再做）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ChunkType = Literal["text", "table", "formula", "figure"]


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: str
    spec_id: str
    section_path: tuple[str, ...]
    section_title: str
    chunk_type: str
    content: str
    # 三类分数：dense / sparse 各自的原始分；rerank 之后的分；fused = RRF / 其他融合后总分
    score_dense: float | None = None
    score_sparse: float | None = None
    score_rerank: float | None = None
    fused_score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def preview(self, *, max_chars: int = 200) -> str:
        text = self.content.strip().replace("\n", " ")
        return text[:max_chars] + ("…" if len(text) > max_chars else "")
