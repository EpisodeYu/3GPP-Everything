"""Hybrid 融合（RRF, Reciprocal Rank Fusion）。

公式 `score = sum_{i in lists} 1 / (k + rank_i)`；rank 从 1 开始。
跨 list 排重按 chunk_id；输出按 fused_score 降序，**只保留 top_n**，原始
score_dense / score_sparse 在融合后的 RetrievedChunk 上同时保留（取最先出现的）。

设计：纯函数，无副作用。可在任意路径调用（agent retrieve_node / lookup / eval）。
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import RetrievedChunk


def rrf_merge(
    *result_lists: Sequence[RetrievedChunk],
    k: int = 60,
    top_n: int | None = None,
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion。

    `result_lists` = 任意条目数的检索结果（dense / sparse / per-query 拆分等）；
    每条按相关性降序传入。返回融合后的 `RetrievedChunk` 列表，按 fused_score 降。

    `top_n=None` 不截断；否则取前 N。
    """
    if not result_lists:
        return []

    merged: dict[str, RetrievedChunk] = {}

    for results in result_lists:
        for rank, item in enumerate(results, start=1):
            contribution = 1.0 / (k + rank)
            existing = merged.get(item.chunk_id)
            if existing is None:
                # 复制一份并注入 fused_score；保留原始 score_dense / score_sparse
                merged[item.chunk_id] = RetrievedChunk(
                    chunk_id=item.chunk_id,
                    spec_id=item.spec_id,
                    section_path=item.section_path,
                    section_title=item.section_title,
                    chunk_type=item.chunk_type,
                    content=item.content,
                    score_dense=item.score_dense,
                    score_sparse=item.score_sparse,
                    score_rerank=item.score_rerank,
                    fused_score=contribution,
                    extra=dict(item.extra),
                )
            else:
                existing.fused_score += contribution
                # 把不同 list 的 score 补全：dense 列表有 score_dense 但 sparse 没有，反之亦然
                if existing.score_dense is None and item.score_dense is not None:
                    existing.score_dense = item.score_dense
                if existing.score_sparse is None and item.score_sparse is not None:
                    existing.score_sparse = item.score_sparse

    ordered = sorted(merged.values(), key=lambda c: c.fused_score, reverse=True)
    if top_n is not None:
        return ordered[:top_n]
    return ordered
