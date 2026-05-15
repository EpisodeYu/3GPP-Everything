"""BM25Writer 单测。

覆盖：
- write_spec_chunks 写 by_spec/{spec_id}.jsonl 内容
- finalize 把所有 by_spec/*.jsonl 合并到 chunks.jsonl + meta.json
- purge_spec 删 by_spec 文件
- 同 spec 重写覆盖（不追加）
- iter_chunks 读取
- 空目录 finalize 不抛
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ingestion.indexer.bm25_writer import BM25Writer


@dataclass(slots=True)
class _Chunk:
    chunk_id: str
    spec_id: str
    spec_number: str = "38.211"
    release: str = "Rel-19"
    series: str = "38"
    clause: str = "5.2.1"
    section_title: str = "Section"
    parent_section_id: str = "p1"
    chunk_type: str = "text"
    document_order: int = 0
    content: str = "some content"
    raw_extra: dict = field(default_factory=dict)
    cross_refs: list[str] = field(default_factory=list)


def _writer(tmp_path: Path) -> BM25Writer:
    return BM25Writer(provider="voyage", base_dir=tmp_path)


def _mk(spec_id: str, n: int) -> list[_Chunk]:
    return [
        _Chunk(chunk_id=f"{spec_id}-{i}", spec_id=spec_id, document_order=i, content=f"text {i}")
        for i in range(n)
    ]


def test_write_spec_chunks_creates_jsonl(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    chunks = _mk("38.211", 3)
    n = w.write_spec_chunks("38.211", chunks)
    assert n == 3
    path = w.by_spec_dir / "38.211.jsonl"
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    rec = json.loads(lines[0])
    assert rec["chunk_id"] == "38.211-0"
    assert rec["content"] == "text 0"
    assert rec["spec_id"] == "38.211"
    assert rec["clause"] == "5.2.1"


def test_write_spec_chunks_overwrites_not_appends(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.write_spec_chunks("38.211", _mk("38.211", 5))
    # 第二次写 3 条 → 应只保留 3
    w.write_spec_chunks("38.211", _mk("38.211", 3))
    path = w.by_spec_dir / "38.211.jsonl"
    assert len(path.read_text().splitlines()) == 3


def test_finalize_merges_by_spec(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.write_spec_chunks("38.211", _mk("38.211", 2))
    w.write_spec_chunks("23.501", _mk("23.501", 3))
    meta = w.finalize()
    assert meta["total_chunks"] == 5
    assert meta["spec_count"] == 2
    assert meta["by_spec"] == {"23.501": 3, "38.211": 2}
    assert w.chunks_file.exists()
    lines = w.chunks_file.read_text().splitlines()
    assert len(lines) == 5
    # 顺序：spec_id 字典序（23.501 在 38.211 前）
    assert json.loads(lines[0])["spec_id"] == "23.501"
    assert json.loads(lines[-1])["spec_id"] == "38.211"


def test_finalize_meta_json_written(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.write_spec_chunks("38.211", _mk("38.211", 1))
    w.finalize()
    meta = json.loads(w.meta_file.read_text())
    assert meta["provider"] == "voyage"
    assert "written_at" in meta
    assert meta["total_chunks"] == 1


def test_purge_spec_deletes_file(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.write_spec_chunks("38.211", _mk("38.211", 1))
    assert (w.by_spec_dir / "38.211.jsonl").exists()
    assert w.purge_spec("38.211") is True
    assert not (w.by_spec_dir / "38.211.jsonl").exists()
    # 再次 purge 返回 False（不存在）
    assert w.purge_spec("38.211") is False


def test_iter_chunks_streams_jsonl(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.write_spec_chunks("38.211", _mk("38.211", 2))
    w.finalize()
    chunks = list(w.iter_chunks())
    assert len(chunks) == 2
    assert chunks[0]["chunk_id"] == "38.211-0"


def test_finalize_empty_dir_safe(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    meta = w.finalize()
    assert meta["total_chunks"] == 0
    assert meta["spec_count"] == 0


def test_list_specs(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    assert w.list_specs() == []
    w.write_spec_chunks("38.211", _mk("38.211", 1))
    w.write_spec_chunks("23.501", _mk("23.501", 1))
    assert w.list_specs() == ["23.501", "38.211"]
