"""主入口：SpecBundle → list[Chunk]（plan §3 / §4 编排各模块）。

工作流：

1. garbage_filter.filter_sections     丢 Contents / Foreword / Postal address / 伪 spec_id 等
2. merger.merge_short_siblings        相邻短 sibling 合并到 parent clause
3. 对每个 section：
   a. atomic_blocks.parse_atomic_blocks   切成 paragraph / table / asn1 / formula / figure ...
   b. section_splitter.split_section      贪心 packing + 三级 fallback
   c. 对每片：figure → figure.build_figure_content；其他 → 头部注入 spec/clause/title
   d. 生成 Chunk dataclass，chunk_id = uuid5(spec_id + clause + sha256(content)[:16])
4. 返回 list[Chunk]，按 document_order 升序、同 section 内按 splitter 输出顺序

内存：单 spec 全产出后返回（生产 ~3000 chunks/spec、每 chunk ~1KB → ~3MB，可控）。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ingestion.hf_loader.models import SectionBlock, SpecBundle, SpecManifestEntry

from . import atomic_blocks as atomic_blocks_mod
from .figure import VisionResolver, build_figure_content, extract_figure
from .formula_alt import build_formula_annotation
from .garbage_filter import filter_sections
from .merger import merge_short_siblings
from .models import Chunk
from .section_splitter import SplitPiece, split_section
from .tokenize_utils import DEFAULT_MODEL as DEFAULT_TOKENIZER_MODEL
from .tokenize_utils import count_tokens

log = logging.getLogger(__name__)

_NS_CHUNK = uuid.UUID("8e6e7d2c-3a3f-4b4d-9b8d-3f4d9c0e0001")
_NS_SECTION = uuid.UUID("8e6e7d2c-3a3f-4b4d-9b8d-3f4d9c0e0002")


@dataclass(slots=True)
class ChunkParams:
    """chunker 主入口参数（plan §0 锁定值）。"""

    target_tokens: int = 250
    max_tokens: int = 400
    overlap_tokens: int = 50
    short_section_threshold: int = 200


@dataclass(slots=True)
class BuildStats:
    """chunker 单次 build 的统计，供 CLI / runner 输出。"""

    sections_total: int = 0
    sections_kept: int = 0
    sections_dropped: int = 0
    sections_merged: int = 0
    chunks_total: int = 0
    chunks_by_type: dict[str, int] = field(default_factory=dict)
    drop_reasons: dict[str, int] = field(default_factory=dict)
    figure_count: int = 0
    figure_with_vision: int = 0


def build_chunks(
    bundle: SpecBundle,
    *,
    params: ChunkParams | None = None,
    vision_resolver: VisionResolver | None = None,
) -> tuple[list[Chunk], BuildStats]:
    """主入口：把 SpecBundle 切成 Chunk 列表。

    `vision_resolver=None` 时 figure chunk 用 GSMA 自带描述（plan 方案 Y 的 fallback）。
    """
    p = params or ChunkParams()
    stats = BuildStats(sections_total=len(bundle.sections))

    # 1) 垃圾过滤
    kept, dropped, reasons = filter_sections(bundle.sections)
    stats.sections_kept = len(kept)
    stats.sections_dropped = len(dropped)
    for reason in reasons.values():
        prefix = reason.split(":", 1)[0]
        stats.drop_reasons[prefix] = stats.drop_reasons.get(prefix, 0) + 1

    if not kept:
        log.warning("spec %s: all sections filtered as garbage", bundle.spec_id)
        return [], stats

    # 2) 合并短 sibling
    n_before = len(kept)
    merged_sections = merge_short_siblings(
        kept,
        short_threshold_tokens=p.short_section_threshold,
        target_tokens=p.target_tokens,
        max_tokens=p.max_tokens,
    )
    stats.sections_merged = max(0, n_before - len(merged_sections))

    # 3) 对每个 section 跑 atomic_blocks → splitter → 生成 Chunk
    chunks: list[Chunk] = []
    created_at = datetime.now(UTC)
    for sec in merged_sections:
        sec_chunks = _build_section_chunks(
            sec,
            entry=bundle.entry,
            dataset_revision=bundle.dataset_revision,
            params=p,
            vision_resolver=vision_resolver,
            created_at=created_at,
            stats=stats,
        )
        chunks.extend(sec_chunks)

    # §6.1: spec 级 chunk_id 去重——多 section 共享 parent_section_id（GSMA marker 把
    # `***field***` 等字段名渲染成 `####` 标题，clause="" 且同名 → 同 parent_section_id；
    # 各 section 内一致的描述段经 hash 后 chunk_id 撞），保留首次出现的副本。
    seen_ids: set[str] = set()
    deduped: list[Chunk] = []
    for c in chunks:
        if c.chunk_id in seen_ids:
            continue
        seen_ids.add(c.chunk_id)
        deduped.append(c)
    chunks = deduped

    stats.chunks_total = len(chunks)
    for c in chunks:
        stats.chunks_by_type[c.chunk_type] = stats.chunks_by_type.get(c.chunk_type, 0) + 1

    return chunks, stats


def _build_section_chunks(
    sec: SectionBlock,
    *,
    entry: SpecManifestEntry,
    dataset_revision: str,
    params: ChunkParams,
    vision_resolver: VisionResolver | None,
    created_at: datetime,
    stats: BuildStats,
) -> list[Chunk]:
    """对单个 section 走原子化 → packing → 头部注入 → 构造 Chunk。"""
    blocks = atomic_blocks_mod.parse_atomic_blocks(sec.body)
    if not blocks:
        return []

    # surrounding_paragraph for figure：取 figure 在 blocks 中的前一个 paragraph block（如有）
    surrounding_by_idx: dict[int, str] = {}
    for idx, blk in enumerate(blocks):
        if blk.kind == "figure":
            for back in range(idx - 1, -1, -1):
                if blocks[back].kind == "paragraph":
                    text = blocks[back].text.strip()
                    if text:
                        surrounding_by_idx[idx] = text[:600]
                    break

    pieces = split_section(
        blocks,
        target_tokens=params.target_tokens,
        max_tokens=params.max_tokens,
        overlap_tokens=params.overlap_tokens,
    )
    if not pieces:
        return []

    parent_section_id = _make_parent_section_id(entry.spec_id, sec.clause, sec.section_title)
    parent_section_chars = sec.body_chars
    section_path = _split_section_path(sec.clause)
    image_repo_dir = _spec_image_dir(entry)

    out: list[Chunk] = []
    seen_content: set[str] = set()  # 同 section 内 content dedupe（§6.1）
    figure_idx_iter = iter(_iter_figure_block_indices(blocks))
    for piece in pieces:
        if piece.chunk_type == "figure":
            try:
                blk_idx = next(figure_idx_iter)
            except StopIteration:
                blk_idx = -1
            content, raw_extra = _build_figure_chunk_content(
                piece,
                sec=sec,
                spec_id=entry.spec_id,
                image_repo_dir=image_repo_dir,
                vision_resolver=vision_resolver,
                surrounding_paragraph=surrounding_by_idx.get(blk_idx),
            )
            stats.figure_count += 1
            if raw_extra.get("vision"):
                stats.figure_with_vision += 1
        else:
            content, raw_extra = _build_text_chunk_content(piece, sec=sec, spec_id=entry.spec_id)

        if not content.strip():
            continue
        # §6.1: 同 section 内若 splitter 输出两份完全一致的 packed text（多见于
        # 短 action_list 短语在嵌套结构中重复出现，packing 后等价），跳过后续
        # 副本——chunk_id = uuid5(spec_id|clause|sha256(content)) 会撞且检索价值
        # 重复。pipeline 层 dedupe 仍保留作为 belt-and-braces。
        if content in seen_content:
            continue
        seen_content.add(content)
        chunk = _make_chunk(
            entry=entry,
            sec=sec,
            section_path=section_path,
            parent_section_id=parent_section_id,
            parent_section_chars=parent_section_chars,
            chunk_type=piece.chunk_type,
            content=content,
            raw_extra=raw_extra,
            document_order=len(out),
            dataset_revision=dataset_revision,
            created_at=created_at,
        )
        out.append(chunk)
    return out


def _iter_figure_block_indices(blocks: list) -> list[int]:
    return [i for i, b in enumerate(blocks) if b.kind == "figure"]


def _spec_image_dir(entry: SpecManifestEntry) -> str:
    """从 `entry.raw_md_path` (`marked/<rel>/<series>/<spec_uid>/raw.md`)
    推 spec 同目录前缀。两层兜底：
      1) 用 entry.image_paths[0] 的目录（最稳）
      2) 用 raw_md_path 的 parent
    """
    if entry.image_paths:
        first = entry.image_paths[0].replace("\\", "/")
        if "/" in first:
            return first.rsplit("/", 1)[0]
    if entry.raw_md_path:
        rp = entry.raw_md_path.replace("\\", "/")
        if "/" in rp:
            return rp.rsplit("/", 1)[0]
    return ""


def _build_text_chunk_content(
    piece: SplitPiece, *, sec: SectionBlock, spec_id: str
) -> tuple[str, dict]:
    """非 figure 片：头部注入 [<spec_id> § <clause> <section_title>]。

    若 piece 含 LaTeX 数学（`$...$` / `$$...$$`）或上游抽空公式模式（"defined by
    \\n\\nwhere" 等）→ chunk 末尾追加 alt-text（Formula symbols / 抽空标注）。
    详见 `formula_alt.py` 注释（2026-05-30 ragas uplift handoff §3.4 后续）。
    """
    header = _section_header(spec_id, sec)
    body = piece.text.strip()
    content = f"{header}\n\n{body}" if header else body
    annotation = build_formula_annotation(body)
    if annotation:
        content = f"{content}\n\n{annotation}"
    raw_extra: dict[str, Any] = {
        "source_kinds": piece.source_kinds,
    }
    if annotation:
        raw_extra["has_formula_annotation"] = True
    if piece.extra:
        raw_extra.update(piece.extra)
    return content, raw_extra


def _build_figure_chunk_content(
    piece: SplitPiece,
    *,
    sec: SectionBlock,
    spec_id: str,
    image_repo_dir: str,
    vision_resolver: VisionResolver | None,
    surrounding_paragraph: str | None,
) -> tuple[str, dict]:
    """figure 片：用 figure.py 构造结构化 content。

    markdown 中 `![alt](xxx_img.jpg)` 只携带 basename；vision_resolver / HF 下载
    需要完整 repo_path（`marked/<rel>/<series>/<spec_uid>/xxx_img.jpg`）。
    `image_repo_dir` 由 builder 根据 `entry.raw_md_path` 的 parent 算出后传入；
    本函数负责把 extract.image_path 替换成完整 repo_path（若它不是绝对路径且
    不以 `marked/` 开头）。
    """
    from dataclasses import replace as _dc_replace

    from .models import AtomicBlock

    extract = extract_figure(AtomicBlock(kind="figure", text=piece.text, extra=piece.extra))
    if extract is None:
        return _build_text_chunk_content(piece, sec=sec, spec_id=spec_id)
    extract = _dc_replace(
        extract,
        image_path=_resolve_image_repo_path(extract.image_path, image_repo_dir),
    )
    content, raw_extra = build_figure_content(
        extract,
        spec_id=spec_id,
        clause=sec.clause,
        section_title=sec.section_title,
        surrounding_paragraph=surrounding_paragraph,
        vision_resolver=vision_resolver,
    )
    raw_extra["source_kinds"] = piece.source_kinds
    return content, raw_extra


def _resolve_image_repo_path(image_path: str, image_repo_dir: str) -> str:
    """把 markdown 中的相对 image_path 转换为 HF 完整 repo_path。

    规则：
      - 空字符串 → 原样返回
      - 绝对路径（本地文件） → 原样返回
      - 已带 `marked/` 前缀 → 原样返回
      - 否则 → `{image_repo_dir}/{basename}`
    """
    if not image_path:
        return image_path
    p = image_path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/") or p.startswith("marked/"):
        return p
    base = (image_repo_dir or "").replace("\\", "/").rstrip("/")
    if not base:
        return p
    return f"{base}/{p.rsplit('/', 1)[-1]}"


def _section_header(spec_id: str, sec: SectionBlock) -> str:
    title = (sec.section_title or "").strip()
    clause = (sec.clause or "").strip()
    if clause and title:
        return f"[{spec_id} § {clause} {title}]"
    if title:
        return f"[{spec_id} § {title}]"
    if clause:
        return f"[{spec_id} § {clause}]"
    return f"[{spec_id}]"


def _make_chunk(
    *,
    entry: SpecManifestEntry,
    sec: SectionBlock,
    section_path: tuple[str, ...],
    parent_section_id: str,
    parent_section_chars: int,
    chunk_type: str,
    content: str,
    raw_extra: dict,
    document_order: int,
    dataset_revision: str,
    created_at: datetime,
) -> Chunk:
    chunk_id = _make_chunk_id(entry.spec_id, sec.clause, content)
    return Chunk(
        chunk_id=chunk_id,
        spec_id=entry.spec_id,
        spec_uid=entry.spec_uid,
        spec_number=entry.spec_number,
        spec_type=entry.spec_type,
        release=entry.release,
        series=entry.series,
        title=entry.title or entry.spec_id,
        chunk_type=chunk_type,  # type: ignore[arg-type]
        clause=sec.clause,
        section_path=section_path,
        section_title=sec.section_title,
        parent_section_id=parent_section_id,
        parent_section_chars=parent_section_chars,
        document_order=document_order,
        content=content,
        raw_extra=raw_extra,
        cross_refs=[],
        source="gsma_hf",
        source_version=dataset_revision,
        created_at=created_at,
    )


def _make_chunk_id(spec_id: str, clause: str, content: str) -> str:
    """plan §3 决策：uuid5(spec_id + clause + sha256(content)[:16])。

    跨 dataset_revision 内容不变 → 同 ID（真正幂等）。
    """
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    name = f"{spec_id}|{clause}|{content_hash}"
    return str(uuid.uuid5(_NS_CHUNK, name))


def _make_parent_section_id(spec_id: str, clause: str, section_title: str = "") -> str:
    """small2big 召回 key：uuid5(spec_id + clause + section_title)。

    必须把 title 纳入种子的两个理由：
    1. GSMA marker 解析后部分 Annex / 字母后缀 clause（"5.15.11.5a"）会被
       markdown_parser 归入 clause=""；同 spec 多段空 clause 需要 title 区分。
    2. merger.py 把多个孤儿 sibling 合并到同一 parent clause（如 5.2.1+5.2.2+5.2.3
       和 5.2.6+5.2.7 都成 clause="5.2"，但是两个不相交的合并组），需要 title
       区分这两个合并组。
    """
    name = f"{spec_id}|{clause}|{section_title}"
    return str(uuid.uuid5(_NS_SECTION, name))


def _split_section_path(clause: str) -> tuple[str, ...]:
    """'5.2.1' → ('5','2','1')；空串返回空 tuple。"""
    if not clause:
        return ()
    return tuple(clause.split("."))


def chunk_token_count(chunk: Chunk) -> int:
    """对外暴露的 token 计数辅助；CLI / 集成测用。"""
    return count_tokens(chunk.content)


__all__ = [
    "DEFAULT_TOKENIZER_MODEL",
    "BuildStats",
    "ChunkParams",
    "build_chunks",
    "chunk_token_count",
]
