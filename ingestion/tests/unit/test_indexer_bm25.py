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
    meta = w.finalize(build_index=False)
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
    w.finalize(build_index=False)
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
    w.finalize(build_index=False)
    chunks = list(w.iter_chunks())
    assert len(chunks) == 2
    assert chunks[0]["chunk_id"] == "38.211-0"


def test_finalize_empty_dir_safe(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    meta = w.finalize()
    assert meta["total_chunks"] == 0
    assert meta["spec_count"] == 0
    # 空数据不应该写出 index/
    assert not w.has_persisted_index()


def test_list_specs(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    assert w.list_specs() == []
    w.write_spec_chunks("38.211", _mk("38.211", 1))
    w.write_spec_chunks("23.501", _mk("23.501", 1))
    assert w.list_specs() == ["23.501", "38.211"]


# -------------------- M8: 持久化 BM25 索引 --------------------


def _mk_text_chunks(spec_id: str, texts: list[str]) -> list[_Chunk]:
    return [
        _Chunk(chunk_id=f"{spec_id}-{i}", spec_id=spec_id, document_order=i, content=t)
        for i, t in enumerate(texts)
    ]


def test_finalize_builds_persisted_index(tmp_path: Path) -> None:
    """finalize 默认应产出 index/ 子目录，含 bm25s 所有约定文件。"""
    w = _writer(tmp_path)
    w.write_spec_chunks(
        "38.331",
        _mk_text_chunks(
            "38.331",
            [
                "RRC connection establishment between UE and gNB",
                "UE idle mode behavior and cell reselection rules",
                "Random access procedure with preamble selection",
            ],
        ),
    )
    w.write_spec_chunks(
        "23.501",
        _mk_text_chunks(
            "23.501",
            ["PDU Session establishment over N1 reference point"],
        ),
    )
    meta = w.finalize()

    assert meta["total_chunks"] == 4
    assert meta["index_dir"] == str(w.index_dir)
    assert meta["index_doc_count"] == 4
    assert "index_built_at" in meta
    assert "bm25s_version" in meta

    assert w.has_persisted_index()
    # bm25s.save 产出的关键文件齐全
    for name in (
        "params.index.json",
        "vocab.index.json",
        "data.csc.index.npy",
        "indices.csc.index.npy",
        "indptr.csc.index.npy",
        "corpus.jsonl",
    ):
        assert (w.index_dir / name).exists(), f"missing {name}"


def test_finalize_can_skip_index_build(tmp_path: Path) -> None:
    """build_index=False 时只合并 chunks.jsonl，不写 index/。"""
    w = _writer(tmp_path)
    w.write_spec_chunks("38.211", _mk_text_chunks("38.211", ["alpha beta gamma"]))
    meta = w.finalize(build_index=False)
    assert meta["total_chunks"] == 1
    assert "index_dir" not in meta
    assert not w.has_persisted_index()


def test_finalize_idempotent_index_overwrite(tmp_path: Path) -> None:
    """重复 finalize：index/ 整体被替换，文件数不累积，无 .new/.old 残留。"""
    w = _writer(tmp_path)
    w.write_spec_chunks("38.211", _mk_text_chunks("38.211", ["alpha beta", "gamma delta"]))
    w.finalize()
    first_files = sorted(p.name for p in w.index_dir.iterdir())

    # 改 by_spec 内容再 finalize
    w.write_spec_chunks(
        "38.211", _mk_text_chunks("38.211", ["epsilon zeta", "eta theta", "iota kappa"])
    )
    meta2 = w.finalize()
    second_files = sorted(p.name for p in w.index_dir.iterdir())
    assert first_files == second_files
    assert meta2["index_doc_count"] == 3
    # 暂存目录不应残留
    assert not (w.root / "index.new").exists()
    assert not (w.root / "index.old").exists()


def test_rebuild_index_from_existing_by_spec(tmp_path: Path) -> None:
    """`rebuild_index()`：只读 by_spec/ + chunks.jsonl，重新 build 索引。"""
    w = _writer(tmp_path)
    w.write_spec_chunks(
        "38.331", _mk_text_chunks("38.331", ["one two three", "four five six"])
    )
    w.finalize(build_index=False)
    assert not w.has_persisted_index()

    meta = w.rebuild_index()
    assert w.has_persisted_index()
    assert meta["index_doc_count"] == 2
    assert "bm25s_version" in meta


def test_rebuild_index_without_meta_works(tmp_path: Path) -> None:
    """老数据：只有 by_spec/，没有 meta.json / chunks.jsonl。rebuild 应该补全。"""
    w = _writer(tmp_path)
    w.write_spec_chunks("38.211", _mk_text_chunks("38.211", ["foo bar baz"]))
    # 模拟"老数据"：删 chunks.jsonl + meta.json
    if w.chunks_file.exists():
        w.chunks_file.unlink()
    if w.meta_file.exists():
        w.meta_file.unlink()

    meta = w.rebuild_index()
    assert w.has_persisted_index()
    assert w.chunks_file.exists()
    assert meta["total_chunks"] == 1
    assert meta["spec_count"] == 1
