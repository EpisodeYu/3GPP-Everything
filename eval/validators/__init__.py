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
from .merger import MergeReport, format_merge_report, merge_golden_files
from .stats import (
    CATEGORY_TARGETS,
    SOURCE_TARGETS,
    CategoryRow,
    GoldenStats,
    SourceRow,
    compute_stats,
    format_stats,
)

__all__ = [
    "CATEGORY_ENUM",
    "CATEGORY_TARGETS",
    "LANGUAGE_ENUM",
    "SOURCE_ENUM",
    "SOURCE_TARGETS",
    "CategoryRow",
    "GoldenStats",
    "MergeReport",
    "SourceRow",
    "ValidationIssue",
    "ValidationReport",
    "compute_stats",
    "format_merge_report",
    "format_report",
    "format_stats",
    "merge_golden_files",
    "validate_golden_file",
    "validate_golden_text",
]
