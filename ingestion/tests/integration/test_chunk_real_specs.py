"""真实 GSMA spec 端到端 chunk 集成测试。

默认跳过；需要 `RUN_REAL_CHUNK=1` 启用，且本机已跑过 `ingestion hf-pull`
（INGEST_DATA_DIR 下有 manifest SQLite）。

覆盖的 spec：
- 38.211   物理层（多表多公式，最大 section ~22k tokens）
- 38.331   RRC（最复杂；ASN.1 块 765 个，最大 section ~45k tokens）
- 23.501   System architecture（图片密集，每张图带 GSMA 描述）

断言：
- chunk 数在合理量级
- 没有 chunk 过大（超 max_tokens × 2 视为切分失败）
- ASN.1 / table chunk 边界完整（含 ASN1START/STOP；含 caption + header）
- figure chunk 至少包含 image_path 与 description
- parent_section_id 相同的 chunks 全部来自同一原 section
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ingestion.chunker import ChunkParams, build_chunks, count_tokens
from ingestion.chunker.tokenize_utils import DEFAULT_MODEL
from ingestion.hf_loader import GsmaHfLoader, dedupe_keep_latest
from ingestion.hf_loader.manifest_store import (
    get_meta,
    manifest_session,
    read_entries,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_CHUNK") != "1",
    reason="set RUN_REAL_CHUNK=1 to run real GSMA chunk tests (requires hf-pull manifest)",
)


def _manifest_path() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


def _load_bundle(spec_id: str):
    manifest = _manifest_path()
    if not manifest.exists():
        pytest.skip(f"manifest not found: {manifest}; run `ingestion hf-pull` first")
    with manifest_session(manifest) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")
    candidates = [e for e in entries if e.spec_id == spec_id]
    if not candidates:
        pytest.skip(f"spec {spec_id} not in manifest")
    entry = dedupe_keep_latest(candidates)[0]
    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)
    bundles = list(loader.iter_specs([entry]))
    assert len(bundles) == 1
    return bundles[0]


def _common_assertions(chunks, *, max_tokens: int) -> None:
    assert chunks, "no chunks produced"

    # 非 figure chunk：不应超过 max_tokens × 2（overlap + 句子边界回溯允许少量超出）
    non_figure_too_big = [
        (c.chunk_id, count_tokens(c.content), c.chunk_type, c.clause)
        for c in chunks
        if c.chunk_type != "figure" and count_tokens(c.content) > max_tokens * 2
    ]
    assert not non_figure_too_big, f"oversize non-figure chunks: {non_figure_too_big[:3]}"

    # figure chunk：GSMA 自带描述长度由数据决定（架构图描述可达 ~2000 tokens）。
    # 仅断言 < Voyage 4-large 上下文上限的安全阈值（4000 tokens，远低于 32k 实际上限）。
    figure_too_big = [
        (c.chunk_id, count_tokens(c.content), c.clause)
        for c in chunks
        if c.chunk_type == "figure" and count_tokens(c.content) > 4000
    ]
    assert not figure_too_big, f"figure chunks exceed embedding-safe limit: {figure_too_big[:3]}"

    # parent_section_id 相同的 chunks 应共享同一 (clause, section_title)
    by_parent: dict[str, set[tuple[str, str]]] = {}
    for c in chunks:
        by_parent.setdefault(c.parent_section_id, set()).add((c.clause, c.section_title))
    for psid, group in by_parent.items():
        assert len(group) == 1, f"parent_section_id {psid} has mixed (clause,title): {group}"


def test_chunk_38211_real() -> None:
    bundle = _load_bundle("38.211")
    params = ChunkParams(target_tokens=250, max_tokens=400, overlap_tokens=50)
    chunks, stats = build_chunks(bundle, params=params)

    print(f"\n[38.211] tokenizer={DEFAULT_MODEL}")
    print(
        f"[38.211] sections total={stats.sections_total} kept={stats.sections_kept} "
        f"dropped={stats.sections_dropped} merged={stats.sections_merged}"
    )
    print(f"[38.211] chunks={stats.chunks_total} by_type={stats.chunks_by_type}")
    print(f"[38.211] drop_reasons={stats.drop_reasons}")

    # 38.211 5861 行，实测 ~300-500 chunk（许多表 / 公式块原子化后体积适中）
    assert 200 <= stats.chunks_total <= 4000, f"chunks={stats.chunks_total}"
    _common_assertions(chunks, max_tokens=400)

    # 表格 chunk 应存在
    table_chunks = [c for c in chunks if c.chunk_type == "table"]
    assert len(table_chunks) > 0, "no table chunks"

    # 表格 chunk 应包含 caption / header / delim 行（至少一类满足）
    for c in table_chunks[:3]:
        assert "|" in c.content


def test_chunk_38331_real() -> None:
    bundle = _load_bundle("38.331")
    params = ChunkParams(target_tokens=250, max_tokens=400, overlap_tokens=50)
    chunks, stats = build_chunks(bundle, params=params)

    print(
        f"\n[38.331] sections total={stats.sections_total} kept={stats.sections_kept} "
        f"dropped={stats.sections_dropped} merged={stats.sections_merged}"
    )
    print(f"[38.331] chunks={stats.chunks_total} by_type={stats.chunks_by_type}")
    print(f"[38.331] drop_reasons={stats.drop_reasons}")

    # 38.331 76k 行，最复杂 spec：~5000-30000 chunks
    assert 3000 <= stats.chunks_total <= 50000, f"chunks={stats.chunks_total}"
    _common_assertions(chunks, max_tokens=400)

    # 765 个 ASN.1 块 → asn1 chunk 应存在
    asn1_chunks = [c for c in chunks if c.chunk_type == "asn1"]
    assert len(asn1_chunks) > 50, f"too few asn1 chunks: {len(asn1_chunks)}"
    # 抽查：每片 ASN.1 chunk content 应含 ASN1START / ASN1STOP（在 raw_extra 内）
    for c in asn1_chunks[:5]:
        assert "ASN1START" in c.content
        assert "ASN1STOP" in c.content

    # action_list chunks（38.331 RRC procedure 描述）
    action_chunks = [c for c in chunks if c.chunk_type == "action_list"]
    assert len(action_chunks) > 0


def test_chunk_23501_real() -> None:
    bundle = _load_bundle("23.501")
    params = ChunkParams(target_tokens=250, max_tokens=400, overlap_tokens=50)
    chunks, stats = build_chunks(bundle, params=params)

    print(
        f"\n[23.501] sections total={stats.sections_total} kept={stats.sections_kept} "
        f"dropped={stats.sections_dropped} merged={stats.sections_merged}"
    )
    print(f"[23.501] chunks={stats.chunks_total} by_type={stats.chunks_by_type}")
    print(f"[23.501] figures={stats.figure_count}")

    assert stats.chunks_total > 100, f"chunks={stats.chunks_total}"
    _common_assertions(chunks, max_tokens=400)

    fig_chunks = [c for c in chunks if c.chunk_type == "figure"]
    assert len(fig_chunks) >= 5, f"too few figure chunks: {len(fig_chunks)}"
    for c in fig_chunks[:3]:
        assert "image_path" in c.raw_extra
        assert "Description:" in c.content


def test_chunk_id_idempotent_across_two_runs() -> None:
    """同一 spec 跑两次应得到完全相同的 chunk_id 列表（plan §3 要求）。"""
    bundle = _load_bundle("38.211")
    params = ChunkParams(target_tokens=250, max_tokens=400, overlap_tokens=50)
    chunks1, _ = build_chunks(bundle, params=params)
    chunks2, _ = build_chunks(bundle, params=params)
    ids1 = [c.chunk_id for c in chunks1]
    ids2 = [c.chunk_id for c in chunks2]
    assert ids1 == ids2
