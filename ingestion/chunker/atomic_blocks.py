"""把 section body 切成 list[AtomicBlock]。

标记类型：paragraph / table / formula_block / figure / asn1 / action_list。
切分策略详见 plan §4.2，要点：

- paragraph     默认（连续非空行）；可被 splitter 自由切。
- table         连续 ≥ 2 行 `|` 起首，含 `|---|` 分隔行；前一行如是
                'Table X.Y-N:' 起首则作为 caption 吸入。整体不切；超长
                由 splitter 调用 split_table_text 按行切，每片重复
                caption + header + delim 行。
- formula_block 含 `$$..$$` 的独立段，整体不切。
- figure        行匹配 `![...](...img...)`；吸收紧邻其后 GSMA 自带
                描述段 + 可选 'Figure X.Y-N:' 标题，整体抽出进 figure
                pipeline。
- asn1          `-- ASN1START` 至 `-- ASN1STOP` 区间（可带 /example/）。
                超长按顶层定义（`Identifier ::=`）切。
- action_list   连续 `- 1>` / `- 2>` / `- N>` 起首的 38.331 RRC procedure
                嵌套动作列表；顶层按 `- 1>` 切；超大单 1> 内部按 `- 2>` 切。

所有原子块以 raw markdown 子串形式保存（含原标记符），不做任何清洗。
clean / 头部注入交给 builder.py。
"""

from __future__ import annotations

import re

from .models import AtomicBlock

_HEADING_RE = re.compile(r"^#{1,6}\s")
_TABLE_PIPE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_TABLE_CAPTION_RE = re.compile(
    r"^\s*\**\s*Table\s+[A-Z]?\d+(?:\.\d+)*(?:-\d+)?[\.:]?\s*:?\s*", re.IGNORECASE
)
_FIGURE_CAPTION_RE = re.compile(
    r"^\s*\**\s*Figure\s+[A-Z]?\d+(?:\.\d+)*(?:-\d+)?\s*[:\.]?\s*", re.IGNORECASE
)
_IMAGE_RE = re.compile(r"^\s*!\[(?P<alt>[^\]]*)\]\((?P<path>[^)]+)\)\s*$")
_ASN1_START_RE = re.compile(r"--\s*(?:/\w+/\s*)?ASN1START\b")
_ASN1_STOP_RE = re.compile(r"--\s*(?:/\w+/\s*)?ASN1STOP\b")
_ACTION_LIST_RE = re.compile(r"^\s*-\s+\d+>\s")
_FORMULA_BLOCK_DELIM_RE = re.compile(r"\$\$")
_FORMULA_INLINE_RE = re.compile(r"\$[^$\n]{1,200}\$")


def _is_table_row(line: str) -> bool:
    return bool(_TABLE_PIPE_RE.match(line))


def _is_table_delim(line: str) -> bool:
    return bool(_TABLE_DELIM_RE.match(line))


def _is_image(line: str) -> bool:
    return bool(_IMAGE_RE.match(line))


def _is_asn1_start(line: str) -> bool:
    return bool(_ASN1_START_RE.search(line))


def _is_asn1_stop(line: str) -> bool:
    return bool(_ASN1_STOP_RE.search(line))


def _is_action_list_line(line: str) -> bool:
    return bool(_ACTION_LIST_RE.match(line))


def parse_atomic_blocks(body: str) -> list[AtomicBlock]:
    """把 section body 切成 AtomicBlock 序列。

    采用单遍扫描 + lookahead；每发现一个特殊块（table/figure/asn1/...）就把它
    封口，剩下的连续段落 / 空行 batch 成 paragraph。
    """
    if not body or not body.strip():
        return []

    lines = body.splitlines()
    n = len(lines)
    blocks: list[AtomicBlock] = []
    para_buf: list[str] = []

    def flush_paragraph() -> None:
        if not para_buf:
            return
        text = "\n".join(para_buf).strip("\n")
        if text.strip():
            blocks.append(AtomicBlock(kind="paragraph", text=text))
        para_buf.clear()

    i = 0
    while i < n:
        line = lines[i]

        # 1. ASN.1 块：先吃 caption 行（如 -- /example/ ASN1START）到 ASN1STOP
        if _is_asn1_start(line):
            flush_paragraph()
            j = i
            while j < n and not _is_asn1_stop(lines[j]):
                j += 1
            # 吃完 ASN1STOP 行
            if j < n:
                j += 1
            block_text = "\n".join(lines[i:j]).rstrip()
            blocks.append(AtomicBlock(kind="asn1", text=block_text))
            i = j
            continue

        # 2. 图片：可能上一行是空行；image line 本身 + 紧邻其后的 GSMA 描述段
        if _is_image(line):
            flush_paragraph()
            j = _consume_figure(lines, i)
            block_text = "\n".join(lines[i:j]).rstrip()
            m = _IMAGE_RE.match(line)
            extra = {
                "image_alt": m.group("alt") if m else "",
                "image_path": m.group("path") if m else "",
            }
            blocks.append(AtomicBlock(kind="figure", text=block_text, extra=extra))
            i = j
            continue

        # 3. 表格：当前行是表格行，且后面 1-2 行内有 delim 行；
        #    前一行如是 Table caption 则把它拉进来
        if _is_table_row(line):
            # lookahead: 找到一个 delim 行（在接下来 3 行内）才确认是表
            has_delim = False
            for k in range(i, min(i + 3, n)):
                if _is_table_delim(lines[k]):
                    has_delim = True
                    break
            if has_delim:
                # 检查 paragraph buffer 末尾是否是表 caption；有则抽出来
                caption: str | None = None
                if para_buf:
                    last = para_buf[-1].strip()
                    if _TABLE_CAPTION_RE.match(last):
                        caption = last
                        para_buf.pop()
                flush_paragraph()
                # 找表的结束：直到不再是 table 行 / delim 行 / 空行
                j = i
                while j < n and (_is_table_row(lines[j]) or not lines[j].strip()):
                    j += 1
                # 去尾部空行
                end = j
                while end > i and not lines[end - 1].strip():
                    end -= 1
                rows = lines[i:end]
                table_text = "\n".join(rows).rstrip()
                if caption:
                    table_text = caption + "\n" + table_text
                blocks.append(
                    AtomicBlock(
                        kind="table",
                        text=table_text,
                        extra={"caption": caption} if caption else {},
                    )
                )
                i = j
                continue

        # 4. action_list（38.331 RRC 嵌套动作）
        if _is_action_list_line(line):
            flush_paragraph()
            j = i
            while j < n:
                cur = lines[j]
                # 吃所有以 "- N>" 起首的行 + 紧跟其下的缩进续行（4 空格起）+ 空行
                if _is_action_list_line(cur):
                    j += 1
                    continue
                if cur.startswith("    ") or cur.startswith("\t"):
                    j += 1
                    continue
                if not cur.strip():
                    # 单空行允许（连续 ≥ 2 个空行才算结束）
                    if (
                        j + 1 < n
                        and lines[j + 1].strip()
                        and not _is_action_list_line(lines[j + 1])
                    ):
                        break
                    j += 1
                    continue
                break
            block_text = "\n".join(lines[i:j]).rstrip()
            blocks.append(AtomicBlock(kind="action_list", text=block_text))
            i = j
            continue

        # 5. formula_block：$$ 包围的整段
        if "$$" in line:
            flush_paragraph()
            # 寻找下一个 $$；同行起止也允许
            count_in_line = line.count("$$")
            if count_in_line >= 2:
                blocks.append(AtomicBlock(kind="formula_block", text=line.rstrip()))
                i += 1
                continue
            j = i + 1
            while j < n and "$$" not in lines[j]:
                j += 1
            if j < n:
                j += 1
            block_text = "\n".join(lines[i:j]).rstrip()
            blocks.append(AtomicBlock(kind="formula_block", text=block_text))
            i = j
            continue

        # 6. paragraph：累积；遇到下一个 heading 兜底（理论上 markdown_parser
        #    已按 heading 切，但若 body 里出现 misc heading 也安全）
        if _HEADING_RE.match(line):
            flush_paragraph()
            blocks.append(AtomicBlock(kind="paragraph", text=line.rstrip()))
            i += 1
            continue

        para_buf.append(line)
        i += 1

    flush_paragraph()
    return blocks


def _consume_figure(lines: list[str], start: int) -> int:
    """从 lines[start] 这一行（image 行）开始，吃完 GSMA 自带描述段 + 可选 Figure caption。

    停止条件（取最先到的）：
    - 下一个 image 行
    - 下一个 heading
    - 连续 2 个空行（视为段落结束）
    - 已经吃到 'Figure X.Y-N:' caption 行后，下一个空行即结束（caption 之后通常
      是与图无关的正文，不应被吸入）
    - 文件结束
    - 出现 ASN1START / table delim 行（明显进了下一个块）

    返回 end index（exclusive）。
    """
    n = len(lines)
    j = start + 1
    blank_run = 0
    seen_figure_caption = False
    while j < n:
        cur = lines[j]
        if _is_image(cur):
            break
        if _HEADING_RE.match(cur):
            break
        if _is_asn1_start(cur):
            break
        if _is_table_delim(cur):
            # 退到表格行起点：通常上一行是表格首行
            j -= 1 if j > start + 1 else 0
            break
        if not cur.strip():
            blank_run += 1
            if blank_run >= 2 or seen_figure_caption:
                break
            j += 1
            continue
        blank_run = 0
        if _FIGURE_CAPTION_RE.match(cur):
            seen_figure_caption = True
        j += 1
    return j


def split_table_text(table_text: str, *, max_rows_per_chunk: int) -> list[str]:
    """超大表的"原子内切片"：按行切，每片重复 caption + 表头 + delim 行。

    `table_text` 形态（包含 caption 在第一行的情况）：
        [Table X.Y-N: ...]\n
        | h1 | h2 |\n
        |----|----|\n
        | a  | b  |\n
        ...

    返回每片的完整 markdown（自带 caption + header + delim + 该片数据行）。
    """
    if max_rows_per_chunk <= 0:
        raise ValueError(f"max_rows_per_chunk must be > 0, got {max_rows_per_chunk}")
    lines = table_text.splitlines()
    # 找 caption（首行不是 | 起首的算 caption；可能没有）
    caption: str | None = None
    body_start = 0
    if lines and not _is_table_row(lines[0]):
        caption = lines[0]
        body_start = 1
        # 可能 caption 后跟空行
        while body_start < len(lines) and not lines[body_start].strip():
            body_start += 1

    body_lines = lines[body_start:]
    if not body_lines:
        return [table_text]

    # 找 delim 行（header 是 delim 的上一行；这里只需要 delim 索引）
    delim_idx = -1
    for k, ln in enumerate(body_lines):
        if _is_table_delim(ln):
            delim_idx = k
            break

    if delim_idx < 0:
        # 没找到 delim，按通用策略一次返回（不该发生，因为是被识别为表才走到这里）
        return [table_text]

    header_block = body_lines[: delim_idx + 1]
    data_rows = body_lines[delim_idx + 1 :]
    # 去掉末尾空行
    while data_rows and not data_rows[-1].strip():
        data_rows.pop()

    if len(data_rows) <= max_rows_per_chunk:
        return [table_text]

    pieces: list[str] = []
    for start in range(0, len(data_rows), max_rows_per_chunk):
        slice_rows = data_rows[start : start + max_rows_per_chunk]
        parts: list[str] = []
        if caption:
            parts.append(caption)
            parts.append("")
        parts.extend(header_block)
        parts.extend(slice_rows)
        pieces.append("\n".join(parts))
    return pieces


def split_asn1_text(asn1_text: str) -> list[str]:
    """超大 ASN.1 块的"原子内切片"。

    策略：按顶层定义（形如 `Identifier ::= ` 或 `Identifier-r19 ::= ` 起首的行）切；
    每片头部保留首条 `-- ASN1START` 标记 + 末尾保留 `-- ASN1STOP` 标记。

    若整块只有 1 个顶层定义（无切点），则原样返回。
    """
    lines = asn1_text.splitlines()
    if not lines:
        return [asn1_text]

    # 找 ASN1START 行 + ASN1STOP 行（可能在首尾，也可能没有）
    start_line = lines[0] if _ASN1_START_RE.search(lines[0]) else "-- ASN1START"
    stop_line = lines[-1] if _ASN1_STOP_RE.search(lines[-1]) else "-- ASN1STOP"

    # 中间内容
    inner_start = 1 if _ASN1_START_RE.search(lines[0]) else 0
    inner_stop = len(lines) - 1 if _ASN1_STOP_RE.search(lines[-1]) else len(lines)
    inner = lines[inner_start:inner_stop]

    # 切点：行起首是 `<Identifier> ::= `（不算前导空白）
    def is_top_def(ln: str) -> bool:
        stripped = ln.lstrip()
        if not stripped:
            return False
        if " ::= " not in stripped[:120]:
            return False
        # 去掉前导空白后，第一个非空白 token 应该是个标识符
        first = stripped.split(None, 1)[0]
        return bool(re.match(r"^[A-Za-z][\w-]*$", first))

    cuts: list[int] = [0]
    for idx in range(1, len(inner)):
        if is_top_def(inner[idx]):
            cuts.append(idx)
    cuts.append(len(inner))

    if len(cuts) <= 2:
        return [asn1_text]

    pieces: list[str] = []
    for k in range(len(cuts) - 1):
        slice_lines = inner[cuts[k] : cuts[k + 1]]
        out = [start_line, *slice_lines, stop_line]
        pieces.append("\n".join(out).rstrip())
    return pieces


_LEVEL_RE = re.compile(r"^\s*-\s+(\d+)>\s")


def _action_level_at(line: str) -> int | None:
    """提取 `- N>` 中的 N；非动作列表行返回 None。"""
    m = _LEVEL_RE.match(line)
    return int(m.group(1)) if m else None


def split_action_list_text(action_text: str, *, max_tokens: int | None = None) -> list[str]:
    """递归式 action_list 切分。

    GSMA marker 输出的 RRC procedure 段经常是 mid-stream 片段（不一定从 `- 1>`
    开始；可能从 `- 4>` 起），所以"按 - 1> 切"的固定策略会失效。改用：
    - 找出本块中存在的最浅 level N_min（即 `- N>` 中 N 最小的那个）
    - 按 N_min 行切
    - 若任一片仍 > max_tokens，递归向更深 level 切
    - 最深仍超就用 split_by_tokens 强切（保证绝对不超）
    """
    from .tokenize_utils import count_tokens

    if max_tokens is None:
        # 兼容旧调用：找最浅 level 切一次，不递归
        return _split_by_min_level(action_text, recurse=False, max_tokens=10**9)

    if count_tokens(action_text) <= max_tokens:
        return [action_text]

    return _split_by_min_level(action_text, recurse=True, max_tokens=max_tokens)


def _split_by_min_level(text: str, *, recurse: bool, max_tokens: int) -> list[str]:
    """按本块中最浅 `- N>` level 切；recurse=True 时对超大子片继续向更深切。"""
    from .tokenize_utils import count_tokens, split_by_tokens

    lines = text.splitlines()
    levels = [lvl for lvl in (_action_level_at(ln) for ln in lines) if lvl is not None]
    if not levels:
        # 完全没有 `- N>` 标记：直接强切
        if recurse and count_tokens(text) > max_tokens:
            return split_by_tokens(text, max_tokens=max_tokens)
        return [text]

    min_level = min(levels)
    cuts = [i for i, ln in enumerate(lines) if _action_level_at(ln) == min_level]
    cuts.append(len(lines))

    if len(cuts) <= 2:
        # 只有 1 个最浅 level 块；尝试更深 level
        if recurse and count_tokens(text) > max_tokens:
            return _split_at_deeper_level(lines, current_min=min_level, max_tokens=max_tokens)
        return [text]

    pieces: list[str] = []
    for k in range(len(cuts) - 1):
        block_lines = lines[cuts[k] : cuts[k + 1]]
        while block_lines and not block_lines[-1].strip():
            block_lines.pop()
        if not block_lines:
            continue
        block_text = "\n".join(block_lines)
        if not recurse or count_tokens(block_text) <= max_tokens:
            pieces.append(block_text)
            continue
        # 子片仍超 max_tokens → 递归更深
        pieces.extend(
            _split_at_deeper_level(block_lines, current_min=min_level, max_tokens=max_tokens)
        )
    return pieces or [text]


def _split_at_deeper_level(lines: list[str], *, current_min: int, max_tokens: int) -> list[str]:
    """current_min 这一层只有 1 个块时，找下一层 level 切；以 current_min 行做锚点。"""
    from .tokenize_utils import count_tokens, split_by_tokens

    if not lines:
        return []

    # 取首行作为 anchor（如 `- 4> if X:`）
    anchor_line = lines[0]
    inner = lines[1:]
    if not inner:
        return ["\n".join(lines)]

    deeper_levels = [
        lvl
        for lvl in (_action_level_at(ln) for ln in inner)
        if lvl is not None and lvl > current_min
    ]
    if not deeper_levels:
        # 没有更深 level 可切：强切整段
        full = "\n".join(lines)
        if count_tokens(full) <= max_tokens:
            return [full]
        return split_by_tokens(full, max_tokens=max_tokens)

    next_level = min(deeper_levels)
    cuts = [i for i, ln in enumerate(inner) if _action_level_at(ln) == next_level]
    cuts.append(len(inner))

    if len(cuts) <= 2:
        # 下一层也只有 1 个块；继续递归（避免死循环：下次取更深）
        return _split_at_deeper_level(
            [anchor_line, *inner], current_min=next_level, max_tokens=max_tokens
        )

    pieces: list[str] = []
    for k in range(len(cuts) - 1):
        sub = inner[cuts[k] : cuts[k + 1]]
        while sub and not sub[-1].strip():
            sub.pop()
        if not sub:
            continue
        sub_text = anchor_line + "\n" + "\n".join(sub)
        if count_tokens(sub_text) <= max_tokens:
            pieces.append(sub_text)
        else:
            # 继续向更深递归
            pieces.extend(
                _split_at_deeper_level(
                    [anchor_line, *sub], current_min=next_level, max_tokens=max_tokens
                )
            )
    return pieces
