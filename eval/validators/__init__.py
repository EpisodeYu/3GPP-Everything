"""eval 校验器集合（M7.0 起）。

入口：`from eval.validators.golden import validate_golden_file`
对应 CLI：`uv run python -m eval.cli golden validate --file <yaml>`
"""

from .golden import (
    CATEGORY_ENUM,
    LANGUAGE_ENUM,
    SOURCE_ENUM,
    ValidationIssue,
    ValidationReport,
    format_report,
    validate_golden_file,
    validate_golden_text,
)

__all__ = [
    "CATEGORY_ENUM",
    "LANGUAGE_ENUM",
    "SOURCE_ENUM",
    "ValidationIssue",
    "ValidationReport",
    "format_report",
    "validate_golden_file",
    "validate_golden_text",
]
