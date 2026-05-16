"""MCQ → 开放问答 LLM 转化模块（T2）。

数据流：
    raw.jsonl/llm_inferred.jsonl 中 inferred_specs ∈ 17 篇 whitelist 的候选
        ↓
    transform.py (mimo-v2.5-pro)
        ↓
    eval/golden/v1.draft.yaml (待人审)
        ↓
    review.py (a/e/r/s 人审 CLI; M3 暂跳，看候选量决定)
        ↓
    eval/golden/v1.yaml

输出 schema 与 docs/03-development/06-evaluation-and-observability.md §3.5 对齐。
"""

from .prompts import (
    TRANSFORM_SYSTEM_PROMPT,
    TransformOutput,
    build_transform_messages,
)

__all__ = [
    "TRANSFORM_SYSTEM_PROMPT",
    "TransformOutput",
    "build_transform_messages",
]
