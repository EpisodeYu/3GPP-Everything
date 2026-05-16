"""LLM prompts（spec 推断 + 后续 T2 MCQ 转化）。

设计原则：
- 严格 JSON 输出，避免解析灾难
- 给 LLM 完整 17 篇 spec whitelist + 一句话主题描述（让推断有"边界感"）
- 鼓励 "expected_specs": [] + reason，不要硬塞
- few-shot 用真实可信的 1-2 例（NEF→23.502 / RRC→38.331）

17 篇 spec 主题速查（来自 3GPP scope page，简化）：
"""

from __future__ import annotations

# spec_id → 一句话主题描述（用于 LLM prompt + 后续人审快速识别）
SPEC_TOPICS: dict[str, str] = {
    "23.401": "EPS architecture (LTE/SAE) — relevant only if Q is about EPC carry-over to 5G",
    "23.501": "5G System (5GS) architecture, NF definitions (AMF/SMF/UPF/...), "
    "PDU Session, slicing concepts",
    "23.502": "5G procedures: Registration, PDU Session establishment, Handover, "
    "Service Request, AMF/SMF interactions",
    "23.503": "5G Policy and Charging Control framework (PCF, UDR), QoS rules",
    "24.501": "5G NAS protocol (UE↔AMF), Registration / Authentication / "
    "PDU Session NAS messages",
    "29.500": "5G Service Based Architecture (SBI) general principles, HTTP/2 "
    "framework, common errors",
    "29.501": "5G OpenAPI / API design / common data types for SBI",
    "29.502": "Nsmf services (SMF service-based interface): PDUSession, " "EventExposure",
    "29.503": "Nudm services (UDM service-based interface): SDM, UECM, " "Authentication, EE",
    "29.518": "Namf services (AMF service-based interface): Communication, "
    "EventExposure, MT, Location",
    "36.213": "LTE PHY procedures (E-UTRA): scheduling, HARQ, CQI, " "power control",
    "38.214": "NR PHY procedures: scheduling, HARQ, CSI, beam management, " "power control",
    "38.300": "NR overall architecture & RAN procedures (UP/CP), " "RRC states, mobility, BWP",
    "38.331": "NR RRC protocol: connection management, RRC IEs, " "RRCReconfiguration, measurement",
    "38.401": "NG-RAN architecture: gNB-CU/DU split (F1), CU-CP/CU-UP (E1), " "Xn interface",
    "38.413": "NG-AP protocol (NG-RAN ↔ AMF on N2): NG Setup, UE Context, " "PDU Session Resource",
    "38.473": "F1AP protocol (gNB-DU ↔ gNB-CU): F1 Setup, UE Context, " "RRC transfer",
}

# 17 篇 spec_id 排序后字符串（prompt 内嵌）
SPEC_LIST_TEXT = "\n".join(f"  - {s}: {t}" for s, t in sorted(SPEC_TOPICS.items()))


SPEC_INFER_SYSTEM_PROMPT = f"""You are a 3GPP standards expert. Given a multiple-choice question \
about telecommunications, your job is to identify which 3GPP specification(s) the question is asking \
about, restricted to the following 17-spec whitelist for the M3 evaluation set.

Whitelist (canonical spec_id → one-line topic):
{SPEC_LIST_TEXT}

Decision rules:
- Output expected_specs ⊆ this whitelist. NEVER include any spec not in the whitelist.
- If the question references a 3GPP spec NOT in the whitelist (e.g. 22.011 V2X, 38.213 PHY, 29.520 \
NWDAF service), set expected_specs to [] and explain in out_of_scope_reason.
- If the question is about non-3GPP standards (IEEE 802.11, IETF RFCs, etc.), set expected_specs to \
[] with out_of_scope_reason "non-3GPP".
- Be conservative: only include a spec if you are confident the answer can be located in that spec.
- Confidence: "high" = answer must be in this spec; "medium" = likely; "low" = guess (these will be \
filtered out).

Output STRICTLY a JSON object, no markdown fences, no preamble, no trailing text:
{{
  "expected_specs": ["23.501", "23.502"],
  "confidence": "high" | "medium" | "low",
  "rationale": "one-sentence reason citing the answer or explanation",
  "out_of_scope_reason": null | "non-3GPP" | "3GPP-but-outside-whitelist" | "no-clear-spec"
}}
"""


SPEC_INFER_USER_TEMPLATE = """Question: {question}

Options:
{options_block}

Correct answer: {answer}

Explanation: {explanation}

Determine the spec(s) this question targets, following the rules above. \
Return only the JSON."""


def build_spec_infer_messages(
    *,
    question: str,
    options: dict[str, str],
    answer: str,
    explanation: str,
) -> list[dict[str, str]]:
    """构造 chat completions messages list（system + user）。"""
    options_block = "\n".join(f"  {k}: {v}" for k, v in options.items()) if options else "  (none)"
    user = SPEC_INFER_USER_TEMPLATE.format(
        question=question.strip(),
        options_block=options_block,
        answer=str(answer or "").strip(),
        explanation=str(explanation or "").strip() or "(none)",
    )
    return [
        {"role": "system", "content": SPEC_INFER_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def options_from_item(item: dict) -> dict[str, str]:
    """从 raw TeleQnA item 抽出 'option N' → text。"""
    return {
        k: str(v)
        for k, v in item.items()
        if isinstance(k, str) and k.startswith("option ") and isinstance(v, str)
    }


__all__ = [
    "SPEC_INFER_SYSTEM_PROMPT",
    "SPEC_INFER_USER_TEMPLATE",
    "SPEC_LIST_TEXT",
    "SPEC_TOPICS",
    "build_spec_infer_messages",
    "options_from_item",
]
