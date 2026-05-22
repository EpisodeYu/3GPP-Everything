"""LiteLLM / Voyage / Tavily 单价表（M7.4）。

文档锚点：`docs/03-development/06-evaluation-and-observability.md §9.1`。

口径：
- 标的是"用尽免费额度后的等效单价"。免费区内的调用 `billed=False` —— usage hook
  会照常累加 token / call 数到 `ApiUsage`，但 `total_cost_usd` 计 0，避免误读
  /admin/stats 上的成本数字。
- LLM：按 input / output token 分别计价（OpenAI 兼容口径）。
- Embedding：按 token 计价（Voyage / GLM 同口径）。
- Rerank：按 Voyage 2024 口径 `query_tokens × n_docs + Σ doc_tokens`，**按 token 不按
  query 次数**；上层 hook 计算总 token 后乘单价即可。
- WebSearch（Tavily）：按调用次数计价（Tavily 自家口径）。

新增模型时只需追加表项；找不到对应模型走 `_UNKNOWN_*`（cost=0），不阻塞 usage 写入。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMPrice:
    """Chat completion 单价（USD per token）。"""

    name: str
    input_per_token: float
    output_per_token: float
    billed: bool = True


@dataclass(frozen=True)
class EmbeddingPrice:
    """Embedding 单价（USD per token，单价不区分 input/output）。"""

    name: str
    per_token: float
    billed: bool = True


@dataclass(frozen=True)
class RerankPrice:
    """Rerank 单价（USD per token，Voyage 口径下 token = query×n_docs + Σdocs）。"""

    name: str
    per_token: float
    billed: bool = True


@dataclass(frozen=True)
class WebSearchPrice:
    """WebSearch 单价（USD per call）。"""

    name: str
    per_call: float
    billed: bool = True


# === LLM ===
# mimo / glm 单价取自 docs/03-development/06-... §9.1 + docs/02-tech-selection.md
_LLM_PRICES: dict[str, LLMPrice] = {
    # Mimo（小米开放平台 mimo-v2.5 系列；价格随官方 list 定期对照）
    "mimo-v2.5-pro": LLMPrice("mimo-v2.5-pro", 1.0 / 1e6, 3.0 / 1e6),
    "mimo-v2.5": LLMPrice("mimo-v2.5", 0.4 / 1e6, 2.0 / 1e6),
    # GLM（智谱）judge 用，避免与 mimo 同源偏差
    "glm-5.1": LLMPrice("glm-5.1", 0.5 / 1e6, 2.0 / 1e6),
    "glm-4-plus": LLMPrice("glm-4-plus", 0.05 / 1e6, 0.15 / 1e6),
}

# === Embedding ===
_EMBEDDING_PRICES: dict[str, EmbeddingPrice] = {
    # Voyage 200M tokens 免费；超出后 $0.12/M。免费区 billed=False → cost=0
    "voyage-4-large": EmbeddingPrice("voyage-4-large", 0.12 / 1e6, billed=False),
    "voyage-3.5": EmbeddingPrice("voyage-3.5", 0.06 / 1e6, billed=False),
    # GLM embedding-3 fallback
    "embedding-3": EmbeddingPrice("embedding-3", 0.5 / 1e6),
}

# === Rerank ===
_RERANK_PRICES: dict[str, RerankPrice] = {
    # Voyage rerank-2.5：200M tokens 免费；超出后 $0.05/M
    "rerank-2.5": RerankPrice("rerank-2.5", 0.05 / 1e6, billed=False),
    "rerank-2": RerankPrice("rerank-2", 0.05 / 1e6, billed=False),
}

# === WebSearch ===
_WEB_SEARCH_PRICES: dict[str, WebSearchPrice] = {
    # Tavily basic search 单价（自家文档口径，2026-05 时点）
    "tavily-search": WebSearchPrice("tavily-search", 0.01),
}

_UNKNOWN_LLM = LLMPrice("_unknown", 0.0, 0.0, billed=False)
_UNKNOWN_EMBEDDING = EmbeddingPrice("_unknown", 0.0, billed=False)
_UNKNOWN_RERANK = RerankPrice("_unknown", 0.0, billed=False)
_UNKNOWN_WEB_SEARCH = WebSearchPrice("_unknown", 0.0, billed=False)


def get_llm_price(model: str) -> LLMPrice:
    return _LLM_PRICES.get(model, _UNKNOWN_LLM)


def get_embedding_price(model: str) -> EmbeddingPrice:
    return _EMBEDDING_PRICES.get(model, _UNKNOWN_EMBEDDING)


def get_rerank_price(model: str) -> RerankPrice:
    return _RERANK_PRICES.get(model, _UNKNOWN_RERANK)


def get_web_search_price(provider: str = "tavily-search") -> WebSearchPrice:
    return _WEB_SEARCH_PRICES.get(provider, _UNKNOWN_WEB_SEARCH)


def llm_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Chat completion 累计成本；免费区 / 未知模型返回 0。"""
    p = get_llm_price(model)
    if not p.billed:
        return 0.0
    return p.input_per_token * max(input_tokens, 0) + p.output_per_token * max(output_tokens, 0)


def embedding_cost_usd(model: str, tokens: int) -> float:
    p = get_embedding_price(model)
    if not p.billed:
        return 0.0
    return p.per_token * max(tokens, 0)


def rerank_cost_usd(
    model: str,
    *,
    query_tokens: int,
    doc_tokens: int,
    n_docs: int,
) -> float:
    """Voyage 口径：billable_tokens = query_tokens × n_docs + Σ doc_tokens。

    单条 rerank 调用按 token 计价（Voyage 2024 起的统一口径，**不是按 query 次数**）。
    免费区 / 未知模型返回 0。
    """
    p = get_rerank_price(model)
    if not p.billed:
        return 0.0
    n = max(n_docs, 0)
    q = max(query_tokens, 0)
    d = max(doc_tokens, 0)
    return p.per_token * (q * n + d)


def rerank_billable_tokens(*, query_tokens: int, doc_tokens: int, n_docs: int) -> int:
    """Voyage 口径下的 billable token 数；usage hook 拿来写入 `embedding_tokens` 列时用。

    注意 ApiUsage schema 没单独的 rerank_tokens 字段（M4.10 设计：rerank 按调用计数），
    但 voyage rerank 实际按 token 计费，本函数把它聚合为单一 billable 数后由 caller
    决定写哪个字段。当前 hook 计入 `total_cost_usd` 而 `rerank_calls` 自增 1。
    """
    return max(query_tokens, 0) * max(n_docs, 0) + max(doc_tokens, 0)


def web_search_cost_usd(provider: str = "tavily-search", calls: int = 1) -> float:
    p = get_web_search_price(provider)
    if not p.billed:
        return 0.0
    return p.per_call * max(calls, 0)


__all__ = [
    "EmbeddingPrice",
    "LLMPrice",
    "RerankPrice",
    "WebSearchPrice",
    "embedding_cost_usd",
    "get_embedding_price",
    "get_llm_price",
    "get_rerank_price",
    "get_web_search_price",
    "llm_cost_usd",
    "rerank_billable_tokens",
    "rerank_cost_usd",
    "web_search_cost_usd",
]
