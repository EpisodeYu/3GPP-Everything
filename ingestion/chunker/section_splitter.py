"""按 plan §4.3 实现单 section 内部的分块器。

输入：经 atomic_blocks 拆好的 list[AtomicBlock]
输出：list[SplitPiece]，每片附带 chunk_type / token 数 / 原子块来源 / extra

参数：
- target_tokens=250  : 目标大小
- max_tokens=400     : 上限（超就要切原子块或回退 fallback）
- overlap_tokens=50  : 相邻 paragraph chunk 的重叠 token 数（按句子边界回溯）

切片策略（贪心 packing + 三级 fallback）：
1. 顺序遍历 atomic blocks，往当前 chunk 累加；当前 + 下一块 ≤ max_tokens → 加入
2. 否则封口当前 chunk
3. 单个 block > max_tokens：
   - table → split_table_text 按行切，每片复制 caption + 表头 + delim
   - asn1  → split_asn1_text 按顶层定义切
   - action_list → split_action_list_text 按 `- 1>` 切
   - paragraph → 三级 fallback：双换行段落 → 句子（. 或 。）→ 强切按 token
   - formula_block / figure → 不切，单独成 chunk（即使超 max_tokens）
4. overlap：相邻"纯 paragraph chunk"复制最后 overlap_tokens（按 token 反推到最近
   句子边界），不跨原子块复制 overlap

注意：figure 不参与 packing（它由 builder.py 单独处理为 figure chunk）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise

from .atomic_blocks import (
    split_action_list_text,
    split_asn1_text,
    split_table_text,
)
from .models import AtomicBlock
from .tokenize_utils import count_tokens, split_by_tokens

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
_PARA_SPLIT_RE = re.compile(r"\n{2,}")

CHUNK_TYPE_BY_KIND = {
    "table": "table",
    "asn1": "asn1",
    "action_list": "action_list",
    "formula_block": "formula",
    "figure": "figure",
    "paragraph": "text",
    "blank": "text",
}


@dataclass(slots=True)
class SplitPiece:
    """splitter 产出的单片中间结果。

    `text` 是 raw markdown 子串（未做 header 注入；那由 builder.py 在生成 Chunk 时
    统一加 `[<spec_id> § <clause> <section_title>]\\n\\n` 前缀）。
    """

    text: str
    chunk_type: str
    source_kinds: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def split_section(
    blocks: list[AtomicBlock],
    *,
    target_tokens: int = 250,
    max_tokens: int = 400,
    overlap_tokens: int = 50,
) -> list[SplitPiece]:
    """对经 atomic_blocks 处理过的 section 跑分块。

    figure 块会被原样 yield 一个 SplitPiece(chunk_type="figure")，留给 builder.py
    用 figure.py 重新构造 content。
    """
    if not blocks:
        return []

    pieces: list[SplitPiece] = []
    pack: list[AtomicBlock] = []
    pack_tokens = 0

    def flush_pack() -> None:
        nonlocal pack, pack_tokens
        if not pack:
            return
        text = "\n\n".join(b.text for b in pack).strip()
        if text:
            kinds = [b.kind for b in pack]
            pieces.append(
                SplitPiece(
                    text=text,
                    chunk_type=_dominant_chunk_type(kinds),
                    source_kinds=kinds,
                )
            )
        pack = []
        pack_tokens = 0

    for block in blocks:
        if block.kind == "figure":
            flush_pack()
            pieces.append(
                SplitPiece(
                    text=block.text,
                    chunk_type="figure",
                    source_kinds=["figure"],
                    extra=dict(block.extra),
                )
            )
            continue

        block_tokens = count_tokens(block.text)

        # 单块就超 max：先封口当前 pack，再对 block 自身切片
        if block_tokens > max_tokens:
            flush_pack()
            sub_pieces = _split_oversized_block(block, max_tokens=max_tokens)
            pieces.extend(sub_pieces)
            continue

        # 原子块（不可与 paragraph 混 pack）：单独成片，但若当前 pack 为空且
        # 此原子块自身 ≤ target，可以单独 yield；否则与小 paragraph 一起 pack
        if block.is_atomic:
            # 原子块允许与已有 pack 合并（如果 pack 都是 paragraph），但
            # 合并后超 max_tokens 就先 flush
            if pack and pack_tokens + block_tokens > max_tokens:
                flush_pack()
            pack.append(block)
            pack_tokens += block_tokens
            # 原子块自带语义边界，达到 target 就立即 flush
            if pack_tokens >= target_tokens:
                flush_pack()
            continue

        # paragraph：累积到接近 target；超 max 就先 flush 再放入
        if pack and pack_tokens + block_tokens > max_tokens:
            flush_pack()
        pack.append(block)
        pack_tokens += block_tokens
        if pack_tokens >= target_tokens:
            flush_pack()

    flush_pack()

    # 加 overlap（仅相邻"纯 paragraph"片）
    if overlap_tokens > 0:
        pieces = _apply_overlap(pieces, overlap_tokens=overlap_tokens)

    # 最终安全网：figure 允许超（GSMA 描述天然原子）；其他类型超 max × 1.5
    # 视为切分逻辑漏网，强切以保证 embedding 不会拒绝。
    return _enforce_size_safety_net(pieces, max_tokens=max_tokens)


def _enforce_size_safety_net(pieces: list[SplitPiece], *, max_tokens: int) -> list[SplitPiece]:
    """对所有非 figure 片做最终大小校验；超 max × 1.5 强切。"""
    safety_limit = int(max_tokens * 1.5)
    out: list[SplitPiece] = []
    for piece in pieces:
        if piece.chunk_type == "figure":
            out.append(piece)
            continue
        if count_tokens(piece.text) <= safety_limit:
            out.append(piece)
            continue
        # 强切；保留同 chunk_type / source_kinds（标注溢出）
        sub_extra = dict(piece.extra)
        sub_extra["force_split_overflow"] = True
        for sub_text in split_by_tokens(piece.text, max_tokens=max_tokens):
            out.append(
                SplitPiece(
                    text=sub_text,
                    chunk_type=piece.chunk_type,
                    source_kinds=piece.source_kinds,
                    extra=sub_extra,
                )
            )
    return out


def _dominant_chunk_type(kinds: list[str]) -> str:
    """决定 packed chunk 的 chunk_type：含原子块时取该原子块类型；纯 paragraph 时为 'text'。"""
    if not kinds:
        return "text"
    # 优先级：figure > asn1 > table > formula > action_list > text
    priority = ["figure", "asn1", "table", "formula_block", "action_list"]
    for p in priority:
        if p in kinds:
            return CHUNK_TYPE_BY_KIND[p]
    return "text"


def _split_oversized_block(block: AtomicBlock, *, max_tokens: int) -> list[SplitPiece]:
    """单块超 max_tokens 时调用对应原子内切片逻辑。"""
    if block.kind == "table":
        # 估算每片可放多少行：先得到表头开销
        rows = _estimate_max_rows_for_table(block.text, max_tokens=max_tokens)
        text_pieces = split_table_text(block.text, max_rows_per_chunk=rows)
        return [SplitPiece(text=t, chunk_type="table", source_kinds=["table"]) for t in text_pieces]

    if block.kind == "asn1":
        text_pieces = split_asn1_text(block.text)
        return [SplitPiece(text=t, chunk_type="asn1", source_kinds=["asn1"]) for t in text_pieces]

    if block.kind == "action_list":
        text_pieces = split_action_list_text(block.text, max_tokens=max_tokens)
        return [
            SplitPiece(text=t, chunk_type="action_list", source_kinds=["action_list"])
            for t in text_pieces
        ]

    if block.kind in {"formula_block", "figure"}:
        # 公式/图片不切，原样返回；figure 会在 split_section 上层处理
        return [
            SplitPiece(
                text=block.text,
                chunk_type=CHUNK_TYPE_BY_KIND[block.kind],
                source_kinds=[block.kind],
                extra=dict(block.extra),
            )
        ]

    # paragraph 三级 fallback
    return _split_paragraph_fallback(block.text, max_tokens=max_tokens)


def _split_paragraph_fallback(text: str, *, max_tokens: int) -> list[SplitPiece]:
    """超大 paragraph：按双换行段落 → 句子 → 强切按 token 三级 fallback。"""
    pieces: list[SplitPiece] = []

    paragraphs = [p.strip() for p in _PARA_SPLIT_RE.split(text) if p.strip()]
    cur: list[str] = []
    cur_tok = 0

    def flush_cur() -> None:
        nonlocal cur, cur_tok
        if cur:
            joined = "\n\n".join(cur).strip()
            if joined:
                pieces.append(
                    SplitPiece(text=joined, chunk_type="text", source_kinds=["paragraph"])
                )
        cur = []
        cur_tok = 0

    for para in paragraphs:
        para_tok = count_tokens(para)
        if para_tok <= max_tokens:
            if cur_tok + para_tok > max_tokens:
                flush_cur()
            cur.append(para)
            cur_tok += para_tok
            continue

        flush_cur()
        # 二级：按句子切
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(para) if s.strip()]
        sub_cur: list[str] = []
        sub_tok = 0
        for sent in sentences:
            s_tok = count_tokens(sent)
            if s_tok > max_tokens:
                # 三级：强切
                if sub_cur:
                    pieces.append(
                        SplitPiece(
                            text=" ".join(sub_cur),
                            chunk_type="text",
                            source_kinds=["paragraph"],
                        )
                    )
                    sub_cur = []
                    sub_tok = 0
                for hard_piece in split_by_tokens(sent, max_tokens=max_tokens):
                    pieces.append(
                        SplitPiece(
                            text=hard_piece,
                            chunk_type="text",
                            source_kinds=["paragraph"],
                        )
                    )
                continue
            if sub_tok + s_tok > max_tokens:
                pieces.append(
                    SplitPiece(
                        text=" ".join(sub_cur),
                        chunk_type="text",
                        source_kinds=["paragraph"],
                    )
                )
                sub_cur = []
                sub_tok = 0
            sub_cur.append(sent)
            sub_tok += s_tok
        if sub_cur:
            pieces.append(
                SplitPiece(text=" ".join(sub_cur), chunk_type="text", source_kinds=["paragraph"])
            )

    flush_cur()
    return pieces


def _estimate_max_rows_for_table(table_text: str, *, max_tokens: int) -> int:
    """估算表格每片能放多少数据行。

    简单做法：测整个表的总 token，按 (data_rows * max / total_tokens) 估；保底 2 行。
    """
    total_tok = count_tokens(table_text)
    if total_tok == 0:
        return 1
    lines = table_text.splitlines()
    data_rows = max(1, len(lines))
    rows_per_chunk = max(2, int(data_rows * max_tokens / total_tok * 0.8))
    return min(rows_per_chunk, data_rows)


def _apply_overlap(pieces: list[SplitPiece], *, overlap_tokens: int) -> list[SplitPiece]:
    """对相邻 'text' chunk（来源全是 paragraph）加 overlap。

    overlap 内容从前一片末尾按 token 反推到最近句子边界；不跨原子块复制。
    """
    if len(pieces) < 2:
        return pieces

    out: list[SplitPiece] = [pieces[0]]
    for prev, cur in pairwise(pieces):
        if (
            prev.chunk_type == "text"
            and cur.chunk_type == "text"
            and "paragraph" in cur.source_kinds
            and "paragraph" in prev.source_kinds
        ):
            tail = _tail_tokens(prev.text, overlap_tokens)
            if tail and not cur.text.startswith(tail):
                merged = SplitPiece(
                    text=tail + "\n\n" + cur.text,
                    chunk_type=cur.chunk_type,
                    source_kinds=cur.source_kinds,
                    extra=cur.extra,
                )
                out.append(merged)
                continue
        out.append(cur)
    return out


def _tail_tokens(text: str, n_tokens: int) -> str:
    """取 text 末尾 ~n_tokens 个 token 对应的子串，回溯到最近句子边界。"""
    if n_tokens <= 0 or not text:
        return ""
    # 整段 token 数 ≤ n_tokens：返回全段
    total = count_tokens(text)
    if total <= n_tokens:
        return text
    # 用 split_by_tokens 反向取末尾片：先估算剩余字符
    pieces = split_by_tokens(text, max_tokens=n_tokens)
    if not pieces:
        return ""
    tail = pieces[-1]
    # 回溯到最近句子边界（在 tail 范围内找最早的句子起点）
    m = list(_SENTENCE_SPLIT_RE.finditer(tail))
    if m:
        # 取第一个句号之后的部分作为 overlap 起点（保证 tail 是完整句子开头）
        start = m[0].end()
        return tail[start:].strip()
    return tail.strip()
