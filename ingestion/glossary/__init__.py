"""术语抽取模块（M4.1）。

从 `21.905` + 各 TS Definitions / Abbreviations 章节抽 term，写入 PG `glossary` 表。
设计口径见 docs/03-development/03-agent.md §0 M4.1。
"""

from .extractor import (
    GlossaryEntry,
    extract_abbreviations,
    extract_bold_definitions,
    extract_from_sections,
    normalize_term,
)
from .writer import PgGlossaryWriter, glossary_table

__all__ = [
    "GlossaryEntry",
    "PgGlossaryWriter",
    "extract_abbreviations",
    "extract_bold_definitions",
    "extract_from_sections",
    "glossary_table",
    "normalize_term",
]
