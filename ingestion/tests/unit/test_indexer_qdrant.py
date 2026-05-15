"""QdrantWriter 单测（用 QdrantClient(":memory:") 内存模式）。

覆盖：
- ensure_collection idempotent（重复调不抛）
- ensure_collection 缺 dim 抛
- payload index 创建（idempotent）
- upsert_chunks 单 batch + 多 batch
- 重跑同 chunk_id → 替换（不重复）
- purge_spec 按 spec_id 删
- count 按 spec_id 过滤
- collection_name_for_provider 默认 / 自定义 prefix
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from qdrant_client import QdrantClient

from ingestion.indexer.qdrant_writer import (
    QdrantWriter,
    collection_name_for_provider,
)


@dataclass(slots=True)
class _Chunk:
    chunk_id: str
    spec_id: str = "38.211"
    spec_uid: str | None = "38211"
    spec_number: str = "38.211"
    spec_type: str = "TS"
    release: str = "Rel-19"
    series: str = "38"
    title: str = "NR; Physical channels"
    chunk_type: str = "text"
    clause: str = "5.2.1"
    section_path: tuple[str, ...] = ("5", "2", "1")
    section_title: str = "Pseudo-random sequence generation"
    parent_section_id: str = "parent-1"
    parent_section_chars: int = 1000
    document_order: int = 0
    content: str = "Some content."
    raw_extra: dict = field(default_factory=dict)
    cross_refs: list[str] = field(default_factory=list)
    source: str = "gsma_hf"
    source_version: str = "rev1"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _fake_chunks(n: int, *, spec_id: str = "38.211") -> list[_Chunk]:
    import uuid as _uuid

    ns = _uuid.uuid5(_uuid.NAMESPACE_URL, f"test|{spec_id}")
    return [
        _Chunk(chunk_id=str(_uuid.uuid5(ns, f"{i}")), spec_id=spec_id, document_order=i)
        for i in range(n)
    ]


def _writer(*, provider: str = "voyage", dim: int = 4) -> QdrantWriter:
    return QdrantWriter(
        client=QdrantClient(":memory:"),
        provider=provider,
        dim=dim,
        collection_name=f"test_chunks_{provider}",
    )


def test_collection_name_for_provider_uses_env_or_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QDRANT_COLLECTION_PREFIX", raising=False)
    assert collection_name_for_provider("voyage") == "tgpp_chunks_voyage"
    monkeypatch.setenv("QDRANT_COLLECTION_PREFIX", "custom_pref")
    assert collection_name_for_provider("glm") == "custom_pref_glm"
    assert collection_name_for_provider("voyage", prefix="explicit") == "explicit_voyage"


def test_ensure_collection_creates_idempotent() -> None:
    w = _writer()
    w.ensure_collection()
    w.ensure_collection()  # 再次调用不应抛
    assert w._collection_ready is True
    assert w.dim == 4


def test_ensure_collection_missing_dim_raises() -> None:
    w = QdrantWriter(client=QdrantClient(":memory:"), provider="voyage", dim=None)
    with pytest.raises(RuntimeError):
        w.ensure_collection()


def test_upsert_and_count_per_spec() -> None:
    w = _writer()
    w.ensure_collection()
    chunks = _fake_chunks(5, spec_id="38.211")
    vectors = [[0.1 * i, 0.2 * i, 0.3 * i, 0.4 * i] for i in range(5)]
    n = w.upsert_chunks(chunks, vectors)
    assert n == 5
    assert w.count() == 5
    assert w.count(spec_id="38.211") == 5
    assert w.count(spec_id="other") == 0


def test_upsert_multi_batch() -> None:
    w = QdrantWriter(
        client=QdrantClient(":memory:"),
        provider="voyage",
        dim=2,
        collection_name="t_multi",
        upsert_batch_size=3,
    )
    w.ensure_collection()
    chunks = _fake_chunks(10)
    vectors = [[float(i), 0.0] for i in range(10)]
    n = w.upsert_chunks(chunks, vectors)
    assert n == 10
    assert w.count() == 10


def test_upsert_same_chunk_id_replaces_no_dup() -> None:
    w = _writer()
    w.ensure_collection()
    chunks = _fake_chunks(3)
    v1 = [[0.1, 0.2, 0.3, 0.4]] * 3
    w.upsert_chunks(chunks, v1)
    assert w.count() == 3
    # 再次 upsert 同 chunk_id（不同 vector） → 替换不增加
    v2 = [[0.5, 0.5, 0.5, 0.5]] * 3
    w.upsert_chunks(chunks, v2)
    assert w.count() == 3


def test_purge_spec_removes_correct_subset() -> None:
    w = _writer()
    w.ensure_collection()
    chunks_a = _fake_chunks(3, spec_id="A.A")
    chunks_b = _fake_chunks(2, spec_id="B.B")
    w.upsert_chunks(chunks_a + chunks_b, [[1.0, 0, 0, 0]] * 5)
    assert w.count() == 5
    removed = w.purge_spec("A.A")
    assert removed == 3
    assert w.count() == 2
    assert w.count(spec_id="B.B") == 2


def test_purge_spec_on_missing_collection_returns_zero() -> None:
    # 还没 ensure_collection，purge 应返回 0 不抛
    w = QdrantWriter(
        client=QdrantClient(":memory:"),
        provider="voyage",
        dim=4,
        collection_name="never_created",
    )
    assert w.purge_spec("X") == 0
    assert w.count() == 0


def test_upsert_validates_length_mismatch() -> None:
    w = _writer()
    w.ensure_collection()
    chunks = _fake_chunks(3)
    with pytest.raises(ValueError):
        w.upsert_chunks(chunks, [[0.1, 0.2, 0.3, 0.4]] * 2)


def test_upsert_without_ensure_collection_raises() -> None:
    w = _writer()
    chunks = _fake_chunks(1)
    with pytest.raises(RuntimeError):
        w.upsert_chunks(chunks, [[0.1, 0.2, 0.3, 0.4]])


def test_payload_round_trips_section_path_and_raw_extra() -> None:
    w = _writer()
    w.ensure_collection()
    c = _fake_chunks(1)[0]
    c.section_path = ("5", "2", "1")
    c.raw_extra = {"image_path": "img.jpg", "vision": {"figure_kind": "architecture"}}
    c.cross_refs = ["xref1"]
    w.upsert_chunks([c], [[0.1, 0.2, 0.3, 0.4]])
    # 抓回 payload 校验
    points, _ = w._client.scroll(collection_name=w.collection_name, with_payload=True, limit=10)
    assert len(points) == 1
    payload = points[0].payload
    assert payload["section_path"] == ["5", "2", "1"]
    assert payload["raw_extra"]["image_path"] == "img.jpg"
    assert payload["raw_extra"]["vision"]["figure_kind"] == "architecture"
    assert payload["cross_refs"] == ["xref1"]
    assert payload["clause"] == "5.2.1"
