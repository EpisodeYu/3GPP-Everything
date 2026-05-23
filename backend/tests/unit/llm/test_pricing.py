"""`app.llm.pricing` 单价表 + 计费函数单测（M7.4）。

覆盖：
- 已知模型走表内单价
- 未知模型走 `_unknown` (cost=0，不抛错)
- 免费区 `billed=False` → cost=0 但 token 仍能计入 caller 数据流
- Voyage rerank 口径 `query_tokens × n_docs + Σ doc_tokens`
"""

from __future__ import annotations

import math

import pytest

from app.llm.pricing import (
    embedding_cost_usd,
    get_embedding_price,
    get_llm_price,
    get_rerank_price,
    get_web_search_price,
    llm_cost_usd,
    rerank_billable_tokens,
    rerank_cost_usd,
    web_search_cost_usd,
)


class TestLLMPrice:
    def test_known_model_billed(self) -> None:
        cost = llm_cost_usd("mimo-v2.5-pro", input_tokens=1_000_000, output_tokens=1_000_000)
        # mimo-v2.5-pro: 1.0/M input, 3.0/M output → 1 + 3 = 4 USD
        assert math.isclose(cost, 4.0, rel_tol=1e-6)

    def test_deepseek_v4_pro_used_as_judge(self) -> None:
        # docs/03-development/06 §3.4：M7.7 起 judge=deepseek-v4-pro
        # cache-miss 档：¥3/M input, ¥6/M output；÷ 7.2 → USD/M
        cost = llm_cost_usd("deepseek-v4-pro", input_tokens=2_000_000, output_tokens=500_000)
        # input: 3/7.2 × 2 = 0.8333；output: 6/7.2 × 0.5 = 0.4167；total ≈ 1.25
        assert math.isclose(cost, (3.0 * 2 + 6.0 * 0.5) / 7.2, rel_tol=1e-6)

    def test_deepseek_v4_flash_cheap_path(self) -> None:
        # cache-miss 档：¥1/M input, ¥2/M output
        cost = llm_cost_usd("deepseek-v4-flash", input_tokens=1_000_000, output_tokens=1_000_000)
        # (1 + 2) / 7.2 ≈ 0.4167
        assert math.isclose(cost, 3.0 / 7.2, rel_tol=1e-6)

    def test_glm_5_1_legacy_entry_preserved(self) -> None:
        # judge 模型 M7.7 已切到 deepseek-v4-pro；GLM 单价表条目保留以便 /admin/stats
        # 正确显示 M7.4-M7.6 期间累积的 api_usage 行（历史 cost 不被改写为 0）
        cost = llm_cost_usd("glm-5.1", input_tokens=2_000_000, output_tokens=500_000)
        assert math.isclose(cost, (6.0 * 2 + 24.0 * 0.5) / 7.2, rel_tol=1e-6)

    def test_glm_4_7_cheapest_short_output_tier(self) -> None:
        # 智谱短输入档 [0, 32K)：¥2/M input, ¥8/M output（[0, 0.2) 输出长度档）
        cost = llm_cost_usd("glm-4.7", input_tokens=1_000_000, output_tokens=1_000_000)
        # (2 + 8) / 7.2 ≈ 1.3889
        assert math.isclose(cost, 10.0 / 7.2, rel_tol=1e-6)

    def test_unknown_model_returns_zero_cost(self) -> None:
        cost = llm_cost_usd("not-a-real-model", input_tokens=10_000, output_tokens=10_000)
        assert cost == 0.0
        assert get_llm_price("not-a-real-model").billed is False

    def test_negative_tokens_clamped(self) -> None:
        assert llm_cost_usd("mimo-v2.5", input_tokens=-100, output_tokens=-50) == 0.0


class TestEmbeddingPrice:
    def test_voyage_free_tier_returns_zero(self) -> None:
        # voyage-4-large 200M tokens 免费 → billed=False → cost=0
        cost = embedding_cost_usd("voyage-4-large", tokens=10_000_000)
        assert cost == 0.0
        p = get_embedding_price("voyage-4-large")
        assert p.billed is False

    def test_glm_embedding_billed(self) -> None:
        cost = embedding_cost_usd("embedding-3", tokens=2_000_000)
        # 0.5/M × 2 = 1.0
        assert math.isclose(cost, 1.0, rel_tol=1e-6)

    def test_unknown_embedding_returns_zero(self) -> None:
        assert embedding_cost_usd("totally-unknown", tokens=100_000) == 0.0


class TestRerankPrice:
    def test_voyage_billable_formula(self) -> None:
        # query × n_docs + Σ doc_tokens = 100 × 5 + 5×800 = 500 + 4000 = 4500
        billable = rerank_billable_tokens(query_tokens=100, doc_tokens=4000, n_docs=5)
        assert billable == 4500

    def test_voyage_rerank_free_tier_returns_zero(self) -> None:
        cost = rerank_cost_usd("rerank-2.5", query_tokens=100, doc_tokens=4000, n_docs=5)
        assert cost == 0.0
        assert get_rerank_price("rerank-2.5").billed is False

    def test_unknown_rerank_returns_zero(self) -> None:
        cost = rerank_cost_usd("fake-rerank-99", query_tokens=100, doc_tokens=200, n_docs=2)
        assert cost == 0.0

    def test_negative_inputs_clamped(self) -> None:
        billable = rerank_billable_tokens(query_tokens=-1, doc_tokens=-100, n_docs=-3)
        assert billable == 0


class TestWebSearchPrice:
    def test_tavily_search_per_call(self) -> None:
        cost = web_search_cost_usd("tavily-search", calls=3)
        # $0.01/call × 3 = $0.03
        assert math.isclose(cost, 0.03, rel_tol=1e-6)

    def test_default_provider_is_tavily(self) -> None:
        assert web_search_cost_usd() == web_search_cost_usd("tavily-search", calls=1)

    def test_unknown_provider_returns_zero(self) -> None:
        cost = web_search_cost_usd("ghost-search", calls=10)
        assert cost == 0.0
        assert get_web_search_price("ghost-search").billed is False


@pytest.mark.parametrize(
    "model,inp,out,expected",
    [
        ("mimo-v2.5", 1_000_000, 0, 0.4),
        ("mimo-v2.5", 0, 1_000_000, 2.0),
        ("mimo-v2.5", 100_000, 100_000, 0.04 + 0.2),
    ],
)
def test_llm_cost_table_consistency(model: str, inp: int, out: int, expected: float) -> None:
    cost = llm_cost_usd(model, input_tokens=inp, output_tokens=out)
    assert math.isclose(cost, expected, rel_tol=1e-6)
