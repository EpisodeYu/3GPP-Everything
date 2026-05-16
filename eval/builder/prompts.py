"""T2 转化 prompt（MCQ → 开放问答）。

schema 来自 docs/03-development/06-evaluation-and-observability.md §3.5：
- rewritten_question: 开放式，不泄露选项，保留 telecom 术语
- expected_specs: list[{spec_id, sections}]，spec_id 必须 ∈ 17 篇 whitelist
- expected_facts: 3-7 个 "答案必须命中的关键事实"（substring 即可）
- forbidden: 0-3 个 "答案不能包含的内容"
- category: definition / procedure / multi_section / table_lookup / formula / tool / negative
- must_say_not_found: 仅 negative 类目时 true
- language: en / zh（默认 en，TeleQnA 全英）
- notes: 可选短注
- skip_reason: 若 LLM 判定不可转化（如"以下哪个不属于"类排除题）→ 给原因

关键约束：
- expected_specs[*].spec_id 必须 ∈ 17 篇 whitelist；外面的会被 transform.py 后处理 reject
- 提供 inferred_specs 作为 hint，让 LLM 沿用 / 修正
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from eval.teleqna.prompts import SPEC_LIST_TEXT

VALID_CATEGORIES: tuple[str, ...] = (
    "definition",
    "procedure",
    "multi_section",
    "table_lookup",
    "formula",
    "tool",
    "negative",
)


TRANSFORM_SYSTEM_PROMPT = f"""You are a 3GPP standards expert helping build a RAG evaluation set. Given a multiple-choice telecom question with its correct answer and explanation, you transform it into an open-ended RAG evaluation item.

Whitelist (canonical spec_id → topic; expected_specs[*].spec_id MUST be in this set):
{SPEC_LIST_TEXT}

Transformation rules:
1. rewritten_question: open-ended; do NOT leak the options or the answer; keep telecom terminology; English.
2. expected_specs: list of {{spec_id, sections}}. spec_id MUST be in the whitelist above. sections is a list of section path prefixes (e.g. "5.3.5", "4.2"); empty list "[]" if you can't pinpoint a section.
3. expected_facts: 3-7 short factual phrases that a correct answer MUST contain (substring or paraphrased keyword); each ≤ 80 chars; English; concrete (e.g. "AMF selection", not "the AMF concept").
4. forbidden: 0-3 phrases the answer must NOT contain (avoid hallucination); leave [] if none.
5. category: pick ONE of: definition | procedure | multi_section | table_lookup | formula | tool | negative
6. must_say_not_found: true ONLY for "negative" category (question references something that doesn't exist in 3GPP scope); false otherwise.
7. language: "en" or "zh"; TeleQnA is English so default "en".
8. notes: optional short note (e.g. "concept defined in §3.1").
9. skip_reason: REQUIRED when you can't reasonably transform (e.g. "exclusion-MCQ" for "which of the following is NOT", "trivia" for questions whose answer is a generic non-spec word). Set skip_reason and leave the other fields default if skipping.

Output STRICTLY a JSON object, no markdown fences, no preamble, no trailing text:
{{
  "rewritten_question": "...",
  "expected_specs": [{{"spec_id": "23.501", "sections": ["5.6"]}}],
  "expected_facts": ["...", "..."],
  "forbidden": [],
  "category": "definition",
  "must_say_not_found": false,
  "language": "en",
  "notes": "",
  "skip_reason": null
}}
"""


TRANSFORM_USER_TEMPLATE = """Original MCQ:

Question: {question}

Options:
{options_block}

Correct answer: {answer}

Explanation: {explanation}

LLM-inferred specs from earlier pass (use as hint; you may revise or extend within the whitelist): {inferred_hint}

Transform to open-ended RAG item per the rules above. Return only the JSON."""


@dataclass(slots=True)
class TransformOutput:
    """转化结果的 dataclass 视图（transform.py 解析 JSON 后填充）。"""

    rewritten_question: str
    expected_specs: list[dict]
    expected_facts: list[str]
    forbidden: list[str]
    category: Literal[
        "definition",
        "procedure",
        "multi_section",
        "table_lookup",
        "formula",
        "tool",
        "negative",
    ]
    must_say_not_found: bool = False
    language: str = "en"
    notes: str = ""
    skip_reason: str | None = None
    raw_response: str | None = field(default=None, compare=False)


def build_transform_messages(
    *,
    question: str,
    options: dict[str, str],
    answer: str,
    explanation: str,
    inferred_specs: list[str] | None = None,
) -> list[dict[str, str]]:
    """构造 chat completions messages list（system + user）。"""
    options_block = "\n".join(f"  {k}: {v}" for k, v in options.items()) if options else "  (none)"
    inferred_hint = ", ".join(inferred_specs) if inferred_specs else "(none)"
    user = TRANSFORM_USER_TEMPLATE.format(
        question=question.strip(),
        options_block=options_block,
        answer=str(answer or "").strip(),
        explanation=str(explanation or "").strip() or "(none)",
        inferred_hint=inferred_hint,
    )
    return [
        {"role": "system", "content": TRANSFORM_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


__all__ = [
    "TRANSFORM_SYSTEM_PROMPT",
    "TRANSFORM_USER_TEMPLATE",
    "VALID_CATEGORIES",
    "TransformOutput",
    "build_transform_messages",
]
