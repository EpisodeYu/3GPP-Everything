"""SparseRetriever 加载 jsonl + BM25 查询。

覆盖：
- legacy in-memory build（fallback 路径）：from_directory 找不到 `index/` 时走旧逻辑
- mmap fast path（M8+）：from_directory 检测到 `index/` 时直接 load + JsonlCorpus
- spec_ids 过滤：单测 / POC 小数据集场景强制 legacy
- 边界：缺目录 / 空 query / 索引文件损坏 → 合理报错或 fallback
"""

from __future__ import annotations

import json
from pathlib import Path

import bm25s  # type: ignore[import-untyped]
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


def _build_persisted_index(dir_path: Path, records: list[dict]) -> None:
    """与 BM25Writer 等价的极小实现：仅供单测；不依赖 ingestion 包。"""
    index_dir = dir_path / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    corpus_texts = [str(r.get("content") or "") for r in records]
    tokens = bm25s.tokenize(corpus_texts, stopwords="en", show_progress=False)
    bm25 = bm25s.BM25()
    bm25.index(tokens, show_progress=False)
    bm25.save(str(index_dir), corpus=records, show_progress=False)


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


# -------------------- M8: mmap fast path --------------------


def test_from_directory_uses_persisted_index(tmp_path: Path) -> None:
    """`index/` 存在时走 mmap fast path，与 legacy 路径检索结果等价。"""
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
    _build_persisted_index(tmp_path, records)

    r = SparseRetriever.from_directory(tmp_path)
    assert r.backend == "mmap"
    assert r.n == 3
    out = r.retrieve("PDU session", top_k=3)
    assert out[0].chunk_id == "c3"
    assert out[0].score_sparse > 0
    assert out[0].section_path == ("6", "2")
    assert out[0].section_title == "PDU Session"


def test_from_directory_falls_back_when_no_index(tmp_path: Path) -> None:
    """无 `index/` 时落到 legacy 路径，结果仍正确。"""
    records = [
        {"chunk_id": "a", "spec_id": "38.331", "clause": "1", "content": "alpha beta"},
        {"chunk_id": "b", "spec_id": "23.501", "clause": "2", "content": "gamma delta"},
    ]
    _write_spec_jsonl(tmp_path, "38.331", records[:1])
    _write_spec_jsonl(tmp_path, "23.501", records[1:])

    r = SparseRetriever.from_directory(tmp_path)
    assert r.backend == "legacy"
    out = r.retrieve("alpha", top_k=2)
    assert out[0].chunk_id == "a"


def test_from_directory_falls_back_when_index_corrupted(tmp_path: Path, caplog) -> None:
    """`index/params.index.json` 存在但损坏 → 不应崩溃，fallback 到 legacy。"""
    records = [{"chunk_id": "a", "spec_id": "x", "clause": "1", "content": "alpha beta gamma"}]
    _write_spec_jsonl(tmp_path, "x", records)

    # 伪造一个无效的 index/
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "params.index.json").write_text("{ this is not valid bm25s index }")

    with caplog.at_level("WARNING"):
        r = SparseRetriever.from_directory(tmp_path)
    assert r.backend == "legacy"
    assert any("unusable" in rec.message or "falling back" in rec.message for rec in caplog.records)
    out = r.retrieve("alpha", top_k=1)
    assert out[0].chunk_id == "a"


def test_spec_ids_filter_forces_legacy(tmp_path: Path) -> None:
    """spec_ids 非空时跳过 fast path，按 spec 子集 build。"""
    records = [
        {"chunk_id": "a", "spec_id": "38.331", "clause": "1", "content": "alpha beta gamma"},
        {"chunk_id": "b", "spec_id": "23.501", "clause": "2", "content": "alpha beta gamma"},
    ]
    _write_spec_jsonl(tmp_path, "38.331", records[:1])
    _write_spec_jsonl(tmp_path, "23.501", records[1:])
    _build_persisted_index(tmp_path, records)

    r = SparseRetriever.from_directory(tmp_path, spec_ids=["38.331"])
    assert r.backend == "legacy"
    assert r.n == 1
    out = r.retrieve("alpha", top_k=1)
    assert out[0].chunk_id == "a"
