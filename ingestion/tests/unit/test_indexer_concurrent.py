"""pipeline_concurrent + multidim 集成单测（M2 §4.8 / B4）。

覆盖：
- happy path：N spec 跨 worker 并行 → 4 路写齐 → multidim 双 collection 一致
- chunk_id 与 sequential `index_spec_multidim` 输出完全一致（幂等）
- vision 预热被调（has resolver, has image_paths）
- 单 spec 失败 → dead-letter 落盘 + 其他 spec 仍成功
- skip_indexed：已存在 spec 不重复 embed
- limiter snapshot 回填到 PipelineStats
- workers=1 退化为 sequential 行为，结果集合一致
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from sqlalchemy import create_engine

from ingestion.hf_loader.models import SectionBlock, SpecBundle, SpecManifestEntry
from ingestion.indexer.bm25_writer import BM25Writer
from ingestion.indexer.embedder import Embedder, _LiteLLMEmbeddingClient
from ingestion.indexer.pg_writer import PgChunkMetaWriter
from ingestion.indexer.pipeline import (
    IndexerComponents,
    index_spec_multidim,
    pipeline_concurrent,
)
from ingestion.indexer.qdrant_writer import QdrantWriter
from ingestion.rate_limit import (
    CompositeLimiter,
    reset_singletons,
    set_mimo_limiter,
    set_voyage_limiter,
)


class _StubHttp(_LiteLLMEmbeddingClient):
    """常量向量 stub；记录请求 dim。"""

    def __init__(self, *, dim_main: int = 8, fail_specs: set[str] | None = None) -> None:
        self.base_url = "http://stub"
        self.api_key = "stub"
        self._owns_client = False
        self._client = None  # type: ignore[assignment]
        self._dim_main = dim_main
        self._fail_specs = fail_specs or set()
        self.calls: list[tuple[int | None, int]] = []  # (requested_dim, n_inputs)

    def embed(self, *, model: str, inputs, dimensions: int | None = None):  # type: ignore[override]
        n = len(inputs)
        self.calls.append((dimensions, n))
        # 模拟基于 inputs 内容触发失败：若任意 input 含 marker 则抛
        for s in inputs:
            for marker in self._fail_specs:
                if marker in s:
                    raise RuntimeError(f"simulated failure for marker={marker}")
        d = dimensions or self._dim_main
        # 用稳定但不全相同的向量（与 input idx 相关）
        return {
            "model": model,
            "data": [{"index": i, "embedding": [0.1 + 0.001 * i] * d} for i in range(n)],
            "usage": {"prompt_tokens": n * 5},
        }


@dataclass
class _RecordedVisionResolver:
    """假 vision resolver；只记录 aresolve_batch 调用。"""

    aresolve_batch_calls: list[list[str]] = field(default_factory=list)

    async def aresolve_batch(self, items, *, concurrent: int = 8):
        self.aresolve_batch_calls.append([p for p, _ in items])
        # 模拟成功预热（返回 None list，sync chunker 不会用到）
        return [None] * len(items)

    def __call__(self, image_path: str, ctx: dict) -> dict | None:  # pragma: no cover
        return None


def _mk_bundle(
    spec_id: str = "38.211",
    *,
    body: str = "Some body text. " * 30,
    image_paths: tuple[str, ...] = (),
) -> SpecBundle:
    entry = SpecManifestEntry(
        spec_uid=spec_id.replace(".", ""),
        spec_id=spec_id,
        spec_number=spec_id,
        spec_type="TS",
        release="Rel-19",
        series="38",
        title=f"TS {spec_id}",
        raw_md_path=f"marked/Rel-19/38_series/{spec_id.replace('.', '')}/raw.md",
        image_paths=image_paths,
        dataset_revision="testrev",
    )
    sec = SectionBlock(
        spec_id=spec_id,
        release="Rel-19",
        clause="5.2.1",
        section_title="Pseudo-random sequence generation",
        section_level=3,
        body=f"[{spec_id}] " + body,  # 让不同 spec 的内容不同 → chunk_id 不同
        body_chars=len(body),
        document_order=0,
    )
    return SpecBundle(entry=entry, sections=[sec], raw_markdown=body, dataset_revision="testrev")


def _components(
    tmp_path: Path,
    *,
    qdrant_client: QdrantClient | None = None,
    fail_markers: set[str] | None = None,
    with_pg: bool = True,
    vision_resolver: Any | None = None,
    dim_main: int = 8,
) -> tuple[IndexerComponents, _StubHttp]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    http = _StubHttp(dim_main=dim_main, fail_specs=fail_markers)
    emb = Embedder(http_client=http, provider="voyage", model="stub", max_retries=0)
    qdrant = QdrantWriter(
        client=qdrant_client or QdrantClient(":memory:"),
        provider="voyage",
        dim=dim_main,
        collection_prefix="t_concurrent",
    )
    bm25 = BM25Writer(provider="voyage", base_dir=tmp_path)
    pg: PgChunkMetaWriter | None = None
    if with_pg:
        # 文件 SQLite：concurrent worker via asyncio.to_thread 跨线程共享同一 DB
        # （`:memory:` 每个连接是独立 DB）
        db_path = tmp_path / "pg.sqlite"
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        pg = PgChunkMetaWriter(engine=engine, provider="voyage")
    comps = IndexerComponents(
        embedder=emb,
        qdrant=qdrant,
        bm25=bm25,
        pg=pg,
        vision_resolver=vision_resolver,
    )
    return comps, http


def _isolated_limiters() -> None:
    """每个测试隔离 mimo/voyage 单例（避免跨测试污染快照）。"""
    reset_singletons()
    set_mimo_limiter(CompositeLimiter(rpm=10000, tpm=None, name="test_mimo"))
    set_voyage_limiter(CompositeLimiter(rpm=10000, tpm=10_000_000, name="test_voyage"))


def test_pipeline_concurrent_writes_all_sinks(tmp_path: Path) -> None:
    _isolated_limiters()
    bundles = [_mk_bundle("38.211"), _mk_bundle("38.331"), _mk_bundle("23.501")]
    comps, _ = _components(tmp_path, dim_main=8)

    async def _go():
        return await pipeline_concurrent(
            bundles,
            comps,
            workers=2,
            dims=[8, 4],
            finalize_bm25=True,
        )

    pstats = asyncio.run(_go())
    assert pstats.specs_attempted == 3
    assert pstats.specs_succeeded == 3
    assert pstats.specs_failed == 0
    assert pstats.chunks_total > 0
    # multidim 双 collection
    assert sorted(pstats.qdrant_upserted_by_dim.keys()) == [4, 8]
    assert pstats.qdrant_upserted_by_dim[8] == pstats.qdrant_upserted_by_dim[4]
    # qdrant 实际计数
    counts = comps.qdrant.count_multidim()
    assert counts[8] == pstats.chunks_total
    assert counts[4] == pstats.chunks_total
    # bm25 finalize 已跑
    meta = comps.bm25.read_meta()
    assert meta is not None
    assert meta["spec_count"] == 3


def test_pipeline_concurrent_chunk_ids_match_sequential(tmp_path: Path) -> None:
    """concurrent 跑出的 chunk_id 集合应 == sequential `index_spec_multidim` 集合。"""
    _isolated_limiters()
    bundles = [_mk_bundle("38.211"), _mk_bundle("38.331")]

    # sequential 路径
    seq_comps, _ = _components(tmp_path / "seq", dim_main=8)
    for b in bundles:
        index_spec_multidim(b, seq_comps, dims=[8, 4])
    seq_pts, _ = seq_comps.qdrant._client.scroll(
        collection_name=seq_comps.qdrant._collections_by_dim[8],
        with_payload=False,
        limit=500,
    )
    seq_ids = {p.id for p in seq_pts}

    # concurrent 路径
    conc_comps, _ = _components(tmp_path / "conc", dim_main=8)

    async def _go():
        return await pipeline_concurrent(bundles, conc_comps, workers=2, dims=[8, 4])

    asyncio.run(_go())
    conc_pts, _ = conc_comps.qdrant._client.scroll(
        collection_name=conc_comps.qdrant._collections_by_dim[8],
        with_payload=False,
        limit=500,
    )
    conc_ids = {p.id for p in conc_pts}

    assert seq_ids == conc_ids
    assert len(seq_ids) > 0


def test_pipeline_concurrent_vision_prefetch_called(tmp_path: Path) -> None:
    _isolated_limiters()
    vision = _RecordedVisionResolver()
    bundle = _mk_bundle(
        "38.211",
        image_paths=("img/a.jpg", "img/b.jpg", "img/c.jpg"),
    )
    comps, _ = _components(tmp_path, vision_resolver=vision, dim_main=8)

    async def _go():
        return await pipeline_concurrent(
            [bundle], comps, workers=1, vision_concurrent=2, dims=[8, 4]
        )

    pstats = asyncio.run(_go())
    assert pstats.specs_succeeded == 1
    assert vision.aresolve_batch_calls == [["img/a.jpg", "img/b.jpg", "img/c.jpg"]]


def test_pipeline_concurrent_no_vision_skip_prefetch(tmp_path: Path) -> None:
    """resolver=None 时不应崩，也不调 prefetch。"""
    _isolated_limiters()
    bundle = _mk_bundle("38.211", image_paths=("x.jpg",))
    comps, _ = _components(tmp_path, vision_resolver=None, dim_main=8)

    async def _go():
        return await pipeline_concurrent([bundle], comps, workers=1, dims=[8, 4])

    pstats = asyncio.run(_go())
    assert pstats.specs_succeeded == 1


def test_pipeline_concurrent_failure_isolated_and_dead_letter(tmp_path: Path) -> None:
    """单 spec 失败 → dead-letter 落盘；其他 spec 仍成功。"""
    _isolated_limiters()
    bundles = [_mk_bundle("38.211"), _mk_bundle("99.999"), _mk_bundle("23.501")]
    # 失败标记基于 chunk content：99.999 spec 的 body 含 "[99.999]"
    comps, _ = _components(tmp_path, fail_markers={"[99.999]"}, dim_main=8)
    dl_dir = tmp_path / "failed"

    async def _go():
        return await pipeline_concurrent(
            bundles, comps, workers=2, dims=[8, 4], dead_letter_dir=dl_dir
        )

    pstats = asyncio.run(_go())
    assert pstats.specs_attempted == 3
    assert pstats.specs_succeeded == 2
    assert pstats.specs_failed == 1
    assert any(spec_id == "99.999" for spec_id, _ in pstats.failures)
    # dead-letter 文件存在
    files = list(dl_dir.glob("*_99.999.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["spec_id"] == "99.999"
    assert payload["error"]
    # 其他 spec 写齐了
    assert pstats.qdrant_upserted_by_dim[8] > 0
    assert pstats.qdrant_upserted_by_dim[4] == pstats.qdrant_upserted_by_dim[8]


def test_pipeline_concurrent_skip_indexed(tmp_path: Path) -> None:
    """skip_indexed=True 时，已有 single-collection point 的 spec 应跳过。

    注：QdrantWriter.count() 用的是 single-collection (self.collection_name)；
    我们用 sequential index_spec_multidim 先写一遍 + 触发 ensure_collections，
    再用 ensure_collection（single）显式写一份判定 collection。
    """
    _isolated_limiters()
    bundle = _mk_bundle("38.211")
    comps, http = _components(tmp_path, dim_main=8)

    # 先跑 sequential 一次
    s = index_spec_multidim(bundle, comps, dims=[8, 4])
    assert s.succeeded
    embed_calls_before = len(http.calls)

    # 用 single-collection 逻辑伪装 "已索引"
    comps.qdrant.collection_name = comps.qdrant._collections_by_dim[8]

    async def _go():
        return await pipeline_concurrent(
            [bundle], comps, workers=1, dims=[8, 4], skip_indexed=True, finalize_bm25=False
        )

    pstats = asyncio.run(_go())
    assert pstats.specs_attempted == 1
    assert pstats.specs_succeeded == 1
    # embed 没再被调（skip 起作用）
    assert len(http.calls) == embed_calls_before


def test_pipeline_concurrent_limiter_snapshot(tmp_path: Path) -> None:
    """跑完后 PipelineStats 回填了 mimo / voyage 限速器快照。"""
    _isolated_limiters()
    # 注入一个已经"用过"的 mimo 限速器，模拟 vision fan-out 后的状态
    fake_mimo = CompositeLimiter(rpm=10000, tpm=None, name="snap_mimo")
    fake_mimo.usage.requests_made = 7
    fake_mimo.usage.tokens_used = 0
    set_mimo_limiter(fake_mimo)
    fake_voyage = CompositeLimiter(rpm=10000, tpm=10_000_000, name="snap_voyage")
    fake_voyage.usage.requests_made = 3
    fake_voyage.usage.tokens_used = 12345
    set_voyage_limiter(fake_voyage)

    bundle = _mk_bundle("38.211")
    comps, _ = _components(tmp_path, dim_main=8)

    async def _go():
        return await pipeline_concurrent([bundle], comps, workers=1, dims=[8, 4])

    pstats = asyncio.run(_go())
    assert pstats.mimo_requests_total == 7
    assert pstats.voyage_requests_total == 3
    assert pstats.voyage_tokens_total == 12345


def test_pipeline_concurrent_workers_one_equals_sequential(tmp_path: Path) -> None:
    """workers=1 时结果应与 sequential 完全一致（chunk_id 集合 + count）。"""
    _isolated_limiters()
    bundles = [_mk_bundle("38.211"), _mk_bundle("23.501")]

    seq_comps, _ = _components(tmp_path / "seq", dim_main=8)
    for b in bundles:
        index_spec_multidim(b, seq_comps, dims=[8, 4])
    seq_count = seq_comps.qdrant.count_multidim()

    conc_comps, _ = _components(tmp_path / "conc1", dim_main=8)

    async def _go():
        return await pipeline_concurrent(bundles, conc_comps, workers=1, dims=[8, 4])

    asyncio.run(_go())
    conc_count = conc_comps.qdrant.count_multidim()
    assert seq_count == conc_count


def test_pipeline_concurrent_empty_bundles(tmp_path: Path) -> None:
    _isolated_limiters()
    comps, _ = _components(tmp_path, dim_main=8)

    async def _go():
        return await pipeline_concurrent([], comps, workers=2, dims=[8, 4])

    pstats = asyncio.run(_go())
    assert pstats.specs_attempted == 0
    assert pstats.specs_succeeded == 0
    assert pstats.chunks_total == 0
