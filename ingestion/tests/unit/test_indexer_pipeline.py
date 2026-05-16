"""pipeline.index_spec / index_specs 端到端编排单测。

用 stub Embedder（mock LiteLLM HTTP）+ in-memory Qdrant + tmp BM25 + in-memory SQLite PG
跑真正的 chunker，验证四方写入一致。

覆盖：
- index_spec 成功路径：chunks > 0 → 4 路全写
- index_spec 失败路径（embedder 抛）→ IndexStats.error 非空，不抛
- index_specs 跨 spec：单 spec 失败不阻塞下一篇
- skip_indexed：Qdrant 已有 point → 跳过
- purge_before=True：旧 chunk_id 被清掉
- finalize_bm25：跑完后 chunks.jsonl 拼好
"""

from __future__ import annotations

from pathlib import Path

from qdrant_client import QdrantClient
from sqlalchemy import create_engine

from ingestion.hf_loader.models import SectionBlock, SpecBundle, SpecManifestEntry
from ingestion.indexer.bm25_writer import BM25Writer
from ingestion.indexer.embedder import Embedder, _LiteLLMEmbeddingClient
from ingestion.indexer.pg_writer import PgChunkMetaWriter
from ingestion.indexer.pipeline import (
    IndexerComponents,
    index_spec,
    index_specs,
)
from ingestion.indexer.qdrant_writer import QdrantWriter


class _StubHttp(_LiteLLMEmbeddingClient):
    """所有 input 都返回长度 4 的常量向量；记录调用次数。"""

    def __init__(self, *, dim: int = 4, fail: bool = False) -> None:
        self.base_url = "http://stub"
        self.api_key = "stub"
        self._owns_client = False
        self._client = None  # type: ignore[assignment]
        self._dim = dim
        self._fail = fail
        self.calls: list[list[str]] = []

    def embed(self, *, model: str, inputs, dimensions: int | None = None):  # type: ignore[override]
        self.calls.append(list(inputs))
        if self._fail:
            raise RuntimeError("simulated embed failure")
        return {
            "model": model,
            "data": [
                {"index": i, "embedding": [0.1 * i, 0.2, 0.3, 0.4][: self._dim]}
                for i in range(len(inputs))
            ],
            "usage": {"prompt_tokens": len(inputs) * 10},
        }


def _mk_bundle(spec_id: str = "38.211", *, body: str = "Some body text. " * 30) -> SpecBundle:
    entry = SpecManifestEntry(
        spec_uid=spec_id.replace(".", ""),
        spec_id=spec_id,
        spec_number=spec_id,
        spec_type="TS",
        release="Rel-19",
        series="38",
        title=f"TS {spec_id}",
        raw_md_path=f"marked/Rel-19/38_series/{spec_id.replace('.', '')}/raw.md",
        dataset_revision="testrev",
    )
    sec = SectionBlock(
        spec_id=spec_id,
        release="Rel-19",
        clause="5.2.1",
        section_title="Pseudo-random sequence generation",
        section_level=3,
        body=body,
        body_chars=len(body),
        document_order=0,
    )
    return SpecBundle(entry=entry, sections=[sec], raw_markdown=body, dataset_revision="testrev")


def _components(
    tmp_path: Path,
    *,
    fail_embed: bool = False,
    provider: str = "voyage",
    with_pg: bool = True,
    qdrant_client: QdrantClient | None = None,
) -> IndexerComponents:
    http = _StubHttp(dim=4, fail=fail_embed)
    emb = Embedder(http_client=http, provider=provider, model="stub-model")
    qdrant = QdrantWriter(
        client=qdrant_client or QdrantClient(":memory:"),
        provider=provider,
        dim=4,
        collection_name=f"t_{provider}",
    )
    bm25 = BM25Writer(provider=provider, base_dir=tmp_path)
    pg: PgChunkMetaWriter | None = None
    if with_pg:
        engine = create_engine("sqlite:///:memory:", future=True)
        pg = PgChunkMetaWriter(engine=engine, provider=provider)
    return IndexerComponents(embedder=emb, qdrant=qdrant, bm25=bm25, pg=pg)


def test_index_spec_writes_to_all_sinks(tmp_path: Path) -> None:
    bundle = _mk_bundle()
    comps = _components(tmp_path)
    stats = index_spec(bundle, comps)
    assert stats.succeeded
    assert stats.chunks_total > 0
    assert stats.qdrant_upserted == stats.chunks_total
    assert stats.bm25_persisted == stats.chunks_total
    assert stats.pg_upserted == stats.chunks_total
    assert stats.vectors_dim == 4
    assert stats.embedding_tokens > 0
    # qdrant
    assert comps.qdrant.count(spec_id="38.211") == stats.chunks_total
    # bm25
    spec_file = comps.bm25.by_spec_dir / "38.211.jsonl"
    assert spec_file.exists()
    assert len(spec_file.read_text().splitlines()) == stats.chunks_total
    # pg
    assert comps.pg.count(spec_id="38.211") == stats.chunks_total


def test_index_spec_returns_failure_when_embedder_raises(tmp_path: Path) -> None:
    bundle = _mk_bundle()
    comps = _components(tmp_path, fail_embed=True)
    stats = index_spec(bundle, comps)
    assert not stats.succeeded
    assert stats.error is not None
    assert "simulated embed failure" in stats.error
    # qdrant / pg / bm25 没写
    # 注意：ensure_collection 是在 embed 之后调的，所以 collection 也没建
    assert comps.qdrant.count() == 0
    assert comps.pg.count() == 0
    assert not (comps.bm25.by_spec_dir / "38.211.jsonl").exists()


def test_index_spec_purge_before_clears_old_chunks(tmp_path: Path) -> None:
    """旧 chunk_id（来自旧内容）应被清掉，新 chunk_id 上位。"""
    bundle1 = _mk_bundle(body="OLD content " * 30)
    bundle2 = _mk_bundle(body="NEW content " * 30)

    qdrant_client = QdrantClient(":memory:")
    comps = _components(tmp_path, qdrant_client=qdrant_client)

    s1 = index_spec(bundle1, comps)
    assert s1.succeeded
    old_count = s1.chunks_total
    assert old_count > 0

    # 拿到 old chunk_ids
    old_points, _ = comps.qdrant._client.scroll(
        collection_name=comps.qdrant.collection_name, with_payload=False, limit=200
    )
    old_ids = {p.id for p in old_points}

    s2 = index_spec(bundle2, comps)
    assert s2.succeeded
    new_points, _ = comps.qdrant._client.scroll(
        collection_name=comps.qdrant.collection_name, with_payload=False, limit=200
    )
    new_ids = {p.id for p in new_points}
    # 旧 ids 应不再存在；新 ids 全部新增
    assert old_ids.isdisjoint(new_ids)
    assert len(new_ids) == s2.chunks_total


def test_index_specs_continues_on_single_failure(tmp_path: Path) -> None:
    """单个 spec embed 失败不该阻塞后续。"""

    class _PartialFailHttp(_LiteLLMEmbeddingClient):
        def __init__(self) -> None:
            self.base_url = "http://stub"
            self.api_key = "stub"
            self._owns_client = False
            self._client = None  # type: ignore[assignment]
            self._call_count = 0

        def embed(self, *, model: str, inputs, dimensions: int | None = None):  # type: ignore[override]
            self._call_count += 1
            # 第一篇（包含 1-2 个 batch）的 embed 调用让其失败，第二篇成功
            if self._call_count == 1:
                raise RuntimeError("first spec boom")
            return {
                "model": model,
                "data": [
                    {"index": i, "embedding": [0.1, 0.2, 0.3, 0.4]} for i in range(len(inputs))
                ],
                "usage": {"prompt_tokens": len(inputs) * 5},
            }

    bundles = [_mk_bundle("38.211"), _mk_bundle("23.501")]
    http = _PartialFailHttp()
    emb = Embedder(http_client=http, provider="voyage", model="stub", max_retries=0)
    qdrant = QdrantWriter(
        client=QdrantClient(":memory:"), provider="voyage", dim=4, collection_name="t_pf"
    )
    bm25 = BM25Writer(provider="voyage", base_dir=tmp_path)
    engine = create_engine("sqlite:///:memory:", future=True)
    pg = PgChunkMetaWriter(engine=engine, provider="voyage")
    comps = IndexerComponents(embedder=emb, qdrant=qdrant, bm25=bm25, pg=pg)

    pstats = index_specs(bundles, comps)
    assert pstats.specs_attempted == 2
    assert pstats.specs_succeeded == 1
    assert pstats.specs_failed == 1
    assert pstats.failures[0][0] == "38.211"
    assert pstats.chunks_total > 0
    # bm25 finalize 已跑（默认 True）
    meta = comps.bm25.read_meta()
    assert meta is not None
    assert meta["spec_count"] == 1


def test_index_specs_skip_indexed(tmp_path: Path) -> None:
    """skip_indexed=True 时，对 Qdrant 已有 point 的 spec 跳过 embedding 调用。"""
    bundle = _mk_bundle()
    comps = _components(tmp_path)

    # 先跑一次填进 Qdrant
    s1 = index_spec(bundle, comps)
    assert s1.succeeded
    before_calls = len(comps.embedder._http.calls)  # type: ignore[attr-defined]

    # 再走 index_specs 带 skip_indexed
    pstats = index_specs([bundle], comps, skip_indexed=True, finalize_bm25=False)
    assert pstats.specs_attempted == 1
    assert pstats.specs_succeeded == 1
    # embed 调用次数应不变
    after_calls = len(comps.embedder._http.calls)  # type: ignore[attr-defined]
    assert after_calls == before_calls


def test_index_spec_empty_chunks_returns_zero(tmp_path: Path) -> None:
    """空 section / 全 garbage → chunks=0；不应崩。"""
    # 用一个空 body 制造 0 chunks（garbage filter 会丢）
    entry = SpecManifestEntry(
        spec_uid="x",
        spec_id="x.y",
        spec_number="x.y",
        spec_type="TS",
        release="Rel-19",
        series="x",
        title="t",
        raw_md_path="m/r",
        dataset_revision="r",
    )
    bundle = SpecBundle(entry=entry, sections=[], raw_markdown="", dataset_revision="r")
    comps = _components(tmp_path)
    stats = index_spec(bundle, comps)
    assert stats.succeeded
    assert stats.chunks_total == 0
    assert stats.qdrant_upserted == 0
    assert stats.bm25_persisted == 0


def test_index_spec_works_without_pg(tmp_path: Path) -> None:
    bundle = _mk_bundle()
    comps = _components(tmp_path, with_pg=False)
    stats = index_spec(bundle, comps)
    assert stats.succeeded
    assert stats.pg_upserted == 0
    assert stats.qdrant_upserted > 0
