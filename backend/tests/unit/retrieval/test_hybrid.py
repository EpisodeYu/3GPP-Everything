"""RRF 融合纯函数测试。"""

from __future__ import annotations

from app.retrieval.hybrid import round_robin_merge, rrf_merge
from app.retrieval.models import RetrievedChunk


def _c(cid: str, *, dense: float | None = None, sparse: float | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        spec_id="38.331",
        section_path=("5", "3"),
        section_title="t",
        chunk_type="text",
        content="x",
        score_dense=dense,
        score_sparse=sparse,
    )


def test_empty_inputs_return_empty() -> None:
    assert rrf_merge() == []
    assert rrf_merge([], []) == []


def test_single_list_preserves_order() -> None:
    a, b, c = _c("a"), _c("b"), _c("c")
    out = rrf_merge([a, b, c], k=60)
    assert [x.chunk_id for x in out] == ["a", "b", "c"]
    assert out[0].fused_score > out[1].fused_score > out[2].fused_score


def test_two_lists_dedup_and_sum() -> None:
    dense = [_c("a", dense=0.9), _c("b", dense=0.7)]
    sparse = [_c("b", sparse=12.0), _c("a", sparse=8.0)]
    out = rrf_merge(dense, sparse, k=60)

    by_id = {c.chunk_id: c for c in out}
    # a 在 dense rank=1, sparse rank=2 → 1/61 + 1/62
    expected_a = 1 / 61 + 1 / 62
    expected_b = 1 / 62 + 1 / 61
    assert abs(by_id["a"].fused_score - expected_a) < 1e-9
    assert abs(by_id["b"].fused_score - expected_b) < 1e-9
    # 双方 score 都补回
    assert by_id["a"].score_dense == 0.9
    assert by_id["a"].score_sparse == 8.0


def test_top_n_truncates() -> None:
    items = [_c(f"x{i}") for i in range(10)]
    out = rrf_merge(items, top_n=3)
    assert len(out) == 3
    assert [c.chunk_id for c in out] == ["x0", "x1", "x2"]


# ---- round_robin_merge（map-reduce reduce 阶段）----


def test_round_robin_empty() -> None:
    assert round_robin_merge([], budget=5) == []
    assert round_robin_merge([[], []], budget=5) == []


def test_round_robin_single_list_preserves_order_and_budget() -> None:
    items = [_c(f"x{i}") for i in range(5)]
    out = round_robin_merge([items], budget=3)
    assert [c.chunk_id for c in out] == ["x0", "x1", "x2"]


def test_round_robin_interleaves_facets() -> None:
    a = [_c("a0"), _c("a1")]
    b = [_c("b0"), _c("b1")]
    c = [_c("c0"), _c("c1")]
    out = round_robin_merge([a, b, c], budget=6)
    assert [x.chunk_id for x in out] == ["a0", "b0", "c0", "a1", "b1", "c1"]


def test_round_robin_top1_of_each_facet_before_any_top2() -> None:
    # facet 公平：budget=3、3 个 facet → 每 facet 恰好它的 top-1 入选
    a = [_c("a0"), _c("a1"), _c("a2")]
    b = [_c("b0")]
    c = [_c("c0"), _c("c1")]
    out = round_robin_merge([a, b, c], budget=3)
    assert [x.chunk_id for x in out] == ["a0", "b0", "c0"]


def test_round_robin_dedup_keeps_first() -> None:
    a = [_c("shared"), _c("a1")]
    b = [_c("shared"), _c("b1")]
    out = round_robin_merge([a, b], budget=10)
    ids = [x.chunk_id for x in out]
    assert ids.count("shared") == 1
    assert ids == ["shared", "a1", "b1"]


def test_round_robin_uneven_lengths_skip_exhausted() -> None:
    a = [_c("a0"), _c("a1"), _c("a2")]
    b = [_c("b0")]
    out = round_robin_merge([a, b], budget=10)
    assert [x.chunk_id for x in out] == ["a0", "b0", "a1", "a2"]


def test_round_robin_budget_zero_no_truncate() -> None:
    a = [_c("a0"), _c("a1")]
    out = round_robin_merge([a], budget=0)
    assert len(out) == 2
