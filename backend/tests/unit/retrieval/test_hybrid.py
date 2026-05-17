"""RRF 融合纯函数测试。"""

from __future__ import annotations

from app.retrieval.hybrid import rrf_merge
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
