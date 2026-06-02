"""单测 `eval.huawei_compare.build_intersection` 纯函数：spec_id 归一 / A·B 抽取 / 交集。"""

from __future__ import annotations

import pytest

from eval.huawei_compare.build_intersection import (
    extract_b_specs,
    normalize_spec_id,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("38331-i00.docx", "38.331"),
        ("23.501.jsonl", "23.501"),
        ("23700-81-i00.docx", "23.700-81"),
        ("TS 24.501 v17", "24.501"),
        ("not-a-spec", None),
        ("", None),
    ],
)
def test_normalize_spec_id(raw: str, expected: str | None) -> None:
    assert normalize_spec_id(raw) == expected


@pytest.mark.unit
def test_extract_b_specs_only_documents_docx() -> None:
    tree = [
        {"type": "file", "path": "Documents/38331-i00.docx"},
        {"type": "file", "path": "Documents/23501-i20.docx"},
        {"type": "file", "path": "Embeddings/EmbeddingsSeries38.npy"},  # 非 Documents → 忽略
        {"type": "file", "path": "Documents.db"},  # 非 docx → 忽略
        {"type": "directory", "path": "Documents"},
    ]
    assert extract_b_specs(tree) == {"38.331", "23.501"}
