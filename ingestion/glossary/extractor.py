"""GSMA raw.md section → list[GlossaryEntry] 的纯函数解析。

3GPP 规范的术语来源稳定地落在两类章节：

1. "Definitions" / "Terms and definitions" 段：每条术语形如 ``**Term:** <定义>``，
   定义可跨多段（含子列表与 NOTE），直到下一个 ``**Term:**`` 或下一级章节标题为止。
2. "Abbreviations" 段：markdown 表格，列 1 = 缩写，列 2 = 全称。

部分 spec 把两段合并到 "Definitions and abbreviations"；按标题白名单同时跑两类解析即可。
不做任何 IO 与外部依赖；调用方（runner）负责加载 SpecBundle 与持久化。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

# section_title.lower() 命中其一即跑定义抽取。
DEFINITION_SECTION_TITLES: frozenset[str] = frozenset(
    {
        "definitions",
        "terms and definitions",
        "terms, definitions and abbreviations",
        "definitions and abbreviations",
        "definitions, symbols and abbreviations",
        "definitions, abbreviations and symbols",
        "definitions, symbols, abbreviations and acronyms",
        "terms, definitions, symbols and abbreviations",
        "terms, definitions, abbreviations and symbols",
    }
)

# section_title.lower() 命中其一即跑缩写表抽取。
ABBREVIATION_SECTION_TITLES: frozenset[str] = frozenset(
    {
        "abbreviations",
        "abbreviations and acronyms",
        "symbols and abbreviations",
        "definitions and abbreviations",
        "terms, definitions and abbreviations",
        "definitions, symbols and abbreviations",
        "definitions, abbreviations and symbols",
        "definitions, symbols, abbreviations and acronyms",
        "terms, definitions, symbols and abbreviations",
        "terms, definitions, abbreviations and symbols",
    }
)

# Definition 起始：行首 `**Term:**`，term 不含 `*` 也不跨行；
# 用 lookahead 把 body 截到下一个 `**Term:**` / 下一个 markdown 标题 / 文档末尾。
_BOLD_DEF_RE = re.compile(
    r"^\*\*(?P<term>[^*\n][^*\n]*?):\*\*[ \t]*(?P<body>.*?)"
    r"(?=^\*\*[^*\n][^*\n]*?:\*\*|^#{1,6}[ \t]+|\Z)",
    re.MULTILINE | re.DOTALL,
)

# 表格行：`| col1 | col2 |`。markdown_parser 已剥掉标题前缀，body 内的表格保持原样。
_TABLE_ROW_RE = re.compile(
    r"^\|[ \t]*(?P<col1>[^|\n]+?)[ \t]*\|[ \t]*(?P<col2>[^|\n]+?)[ \t]*\|[ \t]*$",
    re.MULTILINE,
)

# `term` / `definition` 上限：glossary 表 PG 列 term=VARCHAR(255)、definition=TEXT。
# definition 取实际值不截断（PG TEXT 无上限），但安全网仍设一个；term 严格 255。
_MAX_TERM_CHARS = 255
_MAX_DEFINITION_CHARS = 8000
# 表格列 1 长度阈值：缩写一般 ≤ 24（如 `LP-WUSPS`），> 64 视为误抓多行格式表。
_MAX_ABBR_TERM_CHARS = 64


@dataclass(slots=True)
class GlossaryEntry:
    """单条术语 / 缩写抽取结果。"""

    term: str
    normalized_term: str
    definition: str
    spec_id: str
    section_path: list[str] = field(default_factory=list)
    source_revision: str | None = None


def normalize_term(term: str) -> str:
    """大小写不敏感的精确匹配键。

    规则：
    - strip 两端空白与常见外围标点
    - 内部连续空白压一个空格
    - 全部小写

    示例：``"  PDU  Session "`` → ``"pdu session"``；``"AMF"`` → ``"amf"``。
    """
    s = term.strip().strip("\"'`")
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _clean_definition_body(body: str) -> str:
    """把多段 markdown 定义体压成单段文本但保留可读结构。

    - 去尾部空白与孤立换行
    - 连续空行折叠成一个换行
    - 不动 markdown 列表 / NOTE 行的前导空白
    """
    cleaned = body.rstrip()
    # 多重空行 → 单个空行
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*(?:\n[ \t]*)+", "\n\n", cleaned)
    return cleaned.strip()


def extract_bold_definitions(
    body: str,
    spec_id: str,
    section_path: Iterable[str] | None = None,
    *,
    source_revision: str | None = None,
) -> list[GlossaryEntry]:
    """从 Definitions 段正文抽 ``**Term:** <定义>`` 形式的术语。"""
    out: list[GlossaryEntry] = []
    path = list(section_path) if section_path is not None else []
    for match in _BOLD_DEF_RE.finditer(body):
        raw_term = match.group("term").strip()
        raw_body = _clean_definition_body(match.group("body"))
        if not raw_term or not raw_body:
            continue
        term = raw_term[:_MAX_TERM_CHARS]
        definition = raw_body[:_MAX_DEFINITION_CHARS]
        out.append(
            GlossaryEntry(
                term=term,
                normalized_term=normalize_term(term),
                definition=definition,
                spec_id=spec_id,
                section_path=path,
                source_revision=source_revision,
            )
        )
    return out


def extract_abbreviations(
    body: str,
    spec_id: str,
    section_path: Iterable[str] | None = None,
    *,
    source_revision: str | None = None,
) -> list[GlossaryEntry]:
    """从 Abbreviations 段抓 markdown 表格行 ``| ACR | 全称 |``。"""
    out: list[GlossaryEntry] = []
    path = list(section_path) if section_path is not None else []
    for match in _TABLE_ROW_RE.finditer(body):
        col1 = match.group("col1").strip()
        col2 = match.group("col2").strip()
        if not col1 or not col2:
            continue
        # 分隔行：仅含 `-`、`:`、空格
        if set(col1) <= set("-: ") or set(col2) <= set("-: "):
            continue
        # 表头：col2 形如 "Abbreviation" / "Description" / "Meaning" 等
        if col1.lower() in {"abbreviation", "abbr.", "abbr"} and col2.lower() in {
            "description",
            "meaning",
            "definition",
            "full name",
            "expansion",
        }:
            continue
        if len(col1) > _MAX_ABBR_TERM_CHARS:
            continue
        term = col1[:_MAX_TERM_CHARS]
        definition = col2[:_MAX_DEFINITION_CHARS]
        out.append(
            GlossaryEntry(
                term=term,
                normalized_term=normalize_term(term),
                definition=definition,
                spec_id=spec_id,
                section_path=path,
                source_revision=source_revision,
            )
        )
    return out


class _SectionLike(Protocol):
    clause: str
    section_title: str
    body: str


def extract_from_sections(
    sections: Iterable[_SectionLike],
    spec_id: str,
    *,
    source_revision: str | None = None,
) -> list[GlossaryEntry]:
    """按 section_title 白名单分发到 definition / abbreviation 解析。

    同一 section 同时命中两个白名单（如合并标题 "Definitions and abbreviations"）时，
    两类抽取都跑；bold 定义与 markdown 表格行不会互相误命中（regex 互不重叠）。
    """
    out: list[GlossaryEntry] = []
    for section in sections:
        title_norm = section.section_title.strip().lower()
        if not title_norm:
            continue
        clause = section.clause or section.section_title
        section_path = [clause]
        if title_norm in DEFINITION_SECTION_TITLES:
            out.extend(
                extract_bold_definitions(
                    section.body,
                    spec_id=spec_id,
                    section_path=section_path,
                    source_revision=source_revision,
                )
            )
        if title_norm in ABBREVIATION_SECTION_TITLES:
            out.extend(
                extract_abbreviations(
                    section.body,
                    spec_id=spec_id,
                    section_path=section_path,
                    source_revision=source_revision,
                )
            )
    return out


__all__ = [
    "ABBREVIATION_SECTION_TITLES",
    "DEFINITION_SECTION_TITLES",
    "GlossaryEntry",
    "extract_abbreviations",
    "extract_bold_definitions",
    "extract_from_sections",
    "normalize_term",
]
