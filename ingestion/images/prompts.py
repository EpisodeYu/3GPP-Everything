"""Vision prompts 与 JSON 解析工具。

PROMPT_E_UNIFIED 与 docs/03-development/02-ingestion-and-indexing.md §4.2.1 一字一句对齐。
任何修改必须先在 12 图 mini benchmark（`ingestion/scripts/vision_e_validate.py`）
跑过、确认 JSON 解析成功率 ≥ 95% 后再合并；改文档需同步 §4.2.1。

JSON 解析工具兼容三种 mimo 输出形态：
- 直 JSON（最常见）
- ```json ... ``` 围栏
- 带前后说明文字（截取首个 {...} 块）
"""

from __future__ import annotations

import json
import re

PROMPT_E_UNIFIED = """You are reading a figure extracted from a 3GPP technical specification.

Output STRICT JSON (no prose, no markdown fences):

{
  "figure_kind": "<one of: logo|architecture|message_flow|state_diagram|block_diagram|chart|formula|bit_format|classification|other|undescribable>",
  "visible_labels": ["<every text label / box title / arrow label / axis name / legend item visible, verbatim>"],
  "visible_acronyms": ["<every 3GPP acronym / identifier / function name visible (e.g., AMF, SMF, UPF, N1, AMR, TMGI, MAC-S, f1*, Nudm_SDM_Get), verbatim, deduplicated>"],
  "description": "<see length guidance below>",
  "spec_role": "<short phrase, e.g., 'reference architecture', 'registration message flow', 'state machine for 5GMM', 'authentication function definition'>",
  "undescribable_reason": "<set only when figure_kind=='undescribable'; otherwise empty string.>"
}

Description length guidance (quality over brevity):
- Match the figure's information density. 1-2 sentences for logos / trivial bit
  formats; multiple paragraphs for dense architecture diagrams or long message
  flows. Do NOT truncate substantive content. Do NOT pad with boilerplate.
- Aim for the depth of a careful human caption by the spec author. As a rough
  range, simple figures land near 50-100 tokens; dense architectures or full
  message flows can legitimately reach 800-1500 tokens. Quality over brevity.

Strict rules:
- DO NOT invent labels or acronyms that are not actually visible in the figure.
  If a label is truncated or unreadable, omit it; do NOT guess.
- Preserve every acronym / identifier / function name verbatim. Do NOT expand
  acronyms unless the figure itself spells them out.
- For inferences beyond visible content (figure_kind, spec_role, why the figure
  exists), use weak assertions: 'likely', 'appears to', 'probably represents'.
  NEVER state 3GPP domain knowledge as fact unless it is visible in the figure.
- If the figure is undescribable (corrupted, blank, pure decorative logo with no
  technical info), set figure_kind="undescribable" and explain in
  undescribable_reason. Description should be 1 sentence.
- Output ONLY the JSON object. No surrounding text, no markdown fences."""


VALID_FIGURE_KINDS = frozenset(
    {
        "logo",
        "architecture",
        "message_flow",
        "state_diagram",
        "block_diagram",
        "chart",
        "formula",
        "bit_format",
        "classification",
        "other",
        "undescribable",
    }
)


_FENCE_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_END_RE = re.compile(r"\s*```$")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_vision_json(text: str) -> dict | None:
    """容错解析 mimo vision 输出。

    依次尝试：
      1) 原文直接 json.loads
      2) 去掉 ```json ... ``` 围栏
      3) 截取首个 `{ ... }` 块再解析

    解析失败返回 None；调用方应进 retry 队列。
    """
    if not text:
        return None
    raw = text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    if raw.startswith("```"):
        s = _FENCE_RE.sub("", raw, count=1)
        s = _FENCE_END_RE.sub("", s)
        try:
            return json.loads(s.strip())
        except json.JSONDecodeError:
            pass

    m = _JSON_OBJECT_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def normalize_vision_payload(payload: dict) -> dict | None:
    """把 mimo 输出对齐到 §4.2.2 schema。缺失字段补默认值；非法 figure_kind 退到 'other'。

    入参是 `parse_vision_json` 的结果；返回 dict（含全 6 字段）；
    若 description 完全缺失或为空，认为本次输出不可用 → 返回 None。
    """
    if not isinstance(payload, dict):
        return None
    description = (payload.get("description") or "").strip()
    if not description:
        return None
    figure_kind = (payload.get("figure_kind") or "other").strip().lower()
    if figure_kind not in VALID_FIGURE_KINDS:
        figure_kind = "other"

    def _ensure_str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    return {
        "description": description,
        "figure_kind": figure_kind,
        "visible_labels": _ensure_str_list(payload.get("visible_labels")),
        "visible_acronyms": _ensure_str_list(payload.get("visible_acronyms")),
        "spec_role": (payload.get("spec_role") or "").strip(),
        "undescribable_reason": (payload.get("undescribable_reason") or "").strip(),
    }
