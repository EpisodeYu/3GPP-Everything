"""Hybrid 融合（RRF, Reciprocal Rank Fusion）。

公式 `score = sum_{i in lists} 1 / (k + rank_i)`；rank 从 1 开始。
跨 list 排重按 chunk_id；输出按 fused_score 降序，**只保留 top_n**，原始
score_dense / score_sparse 在融合后的 RetrievedChunk 上同时保留（取最先出现的）。

设计：纯函数，无副作用。可在任意路径调用（agent retrieve_node / lookup / eval）。
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import zip_longest

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


def round_robin_merge(
    ranked_lists: Sequence[Sequence[RetrievedChunk]], *, budget: int
) -> list[RetrievedChunk]:
    """轮转交错合并多个已排序列表，按 chunk_id 去重，截到 budget。

    map-reduce 检索的 reduce 阶段：每个 `ranked_lists[i]` 是某子查询 rerank 后的
    top-m。先取所有 list 的第 0 个，再取所有 list 的第 1 个……保证**每个非空 list 的
    top-1 都先于任意 list 的 top-2 入选**（facet 公平），从而没有强势 facet 把弱
    facet 挤出最终上下文。

    - 去重按 `chunk_id`，保留首次出现（即更靠前 tier / 更靠前 list 的那条）。
    - `budget <= 0` 视为不截断。
    - 空 list 自动跳过；全空 → 返回 []。

    纯函数，无副作用；便于单测。
    """
    if not ranked_lists:
        return []
    seen: set[str] = set()
    out: list[RetrievedChunk] = []
    for tier in zip_longest(*ranked_lists):
        for item in tier:
            if item is None or item.chunk_id in seen:
                continue
            seen.add(item.chunk_id)
            out.append(item)
            if budget > 0 and len(out) >= budget:
                return out
    return out
