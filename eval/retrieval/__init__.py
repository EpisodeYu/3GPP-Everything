"""Retrieval-only 评测模块。

M3 维度决胜（2048 vs 1024）的核心：

- `client.py`    — LiteLLM /embeddings 与 Qdrant 客户端薄封装
- `retriever.py` — Retriever.search(query, dim, top_k) → list[Hit]
- `metrics.py`   — recall@k / MRR / precision@k（spec 级 + section 级）

不依赖 ingestion 内部 API（ingestion/indexer/* 各模块按自己的需要演进，
这里只读 Qdrant collection 已落地的 payload，避免 retrieval 评测被
chunker / embedder 内部接口变动绑架）。
"""

from .client import LiteLLMEmbedClient, get_qdrant_client
from .metrics import RetrievalMetrics, compute_metrics
from .retriever import Hit, Retriever

__all__ = [
    "Hit",
    "LiteLLMEmbedClient",
    "RetrievalMetrics",
    "Retriever",
    "compute_metrics",
    "get_qdrant_client",
]
