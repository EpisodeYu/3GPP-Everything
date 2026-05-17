"""SparseRetriever 加载 jsonl + BM25 查询。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import RetrievalError
from app.retrieval.sparse import SparseRetriever


def _write_spec_jsonl(dir_path: Path, spec: str, records: list[dict]) -> None:
    by_spec = dir_path / "by_spec"
    by_spec.mkdir(parents=True, exist_ok=True)
    path = by_spec / f"{spec}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_from_directory_loads_and_retrieves(tmp_path: Path) -> None:
    records = [
        {
            "chunk_id": "c1",
            "spec_id": "38.331",
            "clause": "5.3.1",
            "section_title": "RRC Connection",
            "chunk_type": "text",
            "content": "RRC connection establishment between UE and gNB",
        },
        {
            "chunk_id": "c2",
            "spec_id": "38.331",
            "clause": "5.3.2",
            "section_title": "Idle Mode",
            "chunk_type": "text",
            "content": "UE idle mode behavior and cell reselection rules",
        },
        {
            "chunk_id": "c3",
            "spec_id": "23.501",
            "clause": "6.2",
            "section_title": "PDU Session",
            "chunk_type": "text",
            "content": "PDU Session establishment over N1 reference point",
        },
    ]
    _write_spec_jsonl(tmp_path, "38.331", records[:2])
    _write_spec_jsonl(tmp_path, "23.501", records[2:])

    r = SparseRetriever.from_directory(tmp_path)
    assert r.n == 3
    out = r.retrieve("PDU session", top_k=3)
    assert out[0].chunk_id == "c3"
    assert out[0].score_sparse > 0
    # section_path 从 clause "6.2" 拆出来
    assert out[0].section_path == ("6", "2")


def test_filter_by_spec_ids(tmp_path: Path) -> None:
    _write_spec_jsonl(
        tmp_path,
        "38.331",
        [{"chunk_id": "a", "spec_id": "38.331", "content": "alpha beta gamma"}],
    )
    _write_spec_jsonl(
        tmp_path,
        "23.501",
        [{"chunk_id": "b", "spec_id": "23.501", "content": "alpha beta gamma"}],
    )
    r = SparseRetriever.from_directory(tmp_path, spec_ids=["38.331"])
    assert r.n == 1
    out = r.retrieve("alpha", top_k=1)
    assert out[0].chunk_id == "a"


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(RetrievalError):
        SparseRetriever.from_directory(tmp_path / "nonexistent")


def test_empty_query_returns_empty(tmp_path: Path) -> None:
    _write_spec_jsonl(tmp_path, "x", [{"chunk_id": "a", "spec_id": "x", "content": "anything"}])
    r = SparseRetriever.from_directory(tmp_path)
    assert r.retrieve("", top_k=5) == []
    assert r.retrieve("   ", top_k=5) == []
