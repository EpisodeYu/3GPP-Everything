"""华为对比测试 · 100 题中立题集的 LLM 生成 prompt（三件套）。

设计锚点（见 README.md §2-3 + CONTEXT.md §7）：
- 题源 = A∩B 的 **R18 共同分母** spec 正文。采样脚手架用 A 的结构化 chunk
  （by_spec/*.jsonl，带 clause + chunk_type=text/table/formula），保证 table/formula
  题型覆盖；每题再去 B 的 R18 全文核验 expected_facts，确保 R18 公平（gen_questions.py）。
- 产出 = golden v1.yaml schema（id/category/language/source/question/expected_specs/
  expected_facts/forbidden/[must_say_not_found]/notes），直接复用 validators/golden.py
  + fact_coverage_judge + negative_judge。

三种 prompt：
1. `build_positive_messages`   — 从一段 spec 正文 chunk 出 definition/procedure/
                                  table_lookup/formula/multi_section 题。
2. `build_false_premise_messages` — negative：编造一个"听起来合理但 3GPP 不存在"的
                                  概念/字段/机制问题（测凭空编造型幻觉）。
3. `build_out_of_lib_messages`    — negative：真实但 R18 库外（Rel-19/20 新特性等）的
                                  问题（测"只答检索到的"还是退回 LLM 记忆瞎答）。

输出一律是**单个 JSON 对象**（无 markdown 包裹更好，但 _extract_json 能剥 ```json 围栏）。
"""

from __future__ import annotations

# gen 阶段允许的 category（negative 由专门 prompt 产，不在 positive 集合里）
POSITIVE_CATEGORIES: tuple[str, ...] = (
    "definition",
    "procedure",
    "multi_section",
    "table_lookup",
    "formula",
)

# chunk_type → 该 chunk 适合出的 category 候选（gen_questions 采样时据此分流）
CHUNK_TYPE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "table": ("table_lookup",),
    "formula": ("formula",),
    "text": ("definition", "procedure", "multi_section"),
}


_POSITIVE_SYSTEM = """You are a 3GPP standards expert building a *neutral* RAG \
evaluation set to compare two independent 3GPP retrieval systems. You write one \
open-ended evaluation question grounded ONLY in the provided spec excerpt.

Hard rules:
- The question MUST be answerable using ONLY the provided excerpt (do not require \
outside knowledge). Both systems retrieve from the same 3GPP corpus, so the answer \
must live in this spec.
- Do NOT leak the answer in the question. No multiple-choice, no "which of the \
following".
- Mention the concept / message / table / parameter by its real 3GPP name so a \
retriever can find it. You MAY name the spec (e.g. "per TS {spec_id}").
- Prefer release-invariant facts (variable names, IE names, table IDs, mechanism \
names) over exact numeric values that may drift across releases.

Return a SINGLE JSON object (no prose, no markdown fence) with keys:
- "question": string, the open-ended question.
- "category": one of {categories}. Pick the most fitting.
- "expected_specs": [{{"spec_id": "{spec_id}", "sections": ["<clause>"]}}] — usually \
just this spec + the excerpt's clause.
- "expected_facts": 3-7 SHORT atomic strings the correct answer must state \
(names / terms / values from the excerpt; each <= 120 chars). For table_lookup / \
formula include the key cells / variables.
- "forbidden": 0-3 SHORT strings a correct answer must NOT contain (hallucination \
traps, e.g. a wrong/LTE spec number, or a concept from a different domain).
- "language": "en".
- "notes": <= 1 short sentence on what the question tests (optional).
- "skip_reason": set ONLY if this excerpt is unusable for a question (boilerplate / \
copyright / references / scope-only); then leave other fields empty."""


_POSITIVE_USER = """Spec: TS {spec_id}  (3GPP)
Clause: {clause}  —  {section_title}
Excerpt:
\"\"\"
{excerpt}
\"\"\"

Write ONE {category_hint} evaluation question grounded only in this excerpt."""


def build_positive_messages(
    *,
    spec_id: str,
    clause: str,
    section_title: str,
    excerpt: str,
    categories: tuple[str, ...],
    category_hint: str,
) -> list[dict[str, str]]:
    """从单段 spec 正文 chunk 出题。`categories` = 该 chunk_type 允许的 category 集合。"""
    cat_str = " / ".join(categories)
    return [
        {
            "role": "system",
            "content": _POSITIVE_SYSTEM.format(categories=cat_str, spec_id=spec_id),
        },
        {
            "role": "user",
            "content": _POSITIVE_USER.format(
                spec_id=spec_id,
                clause=clause or "(n/a)",
                section_title=section_title or "(n/a)",
                excerpt=excerpt,
                category_hint=category_hint,
            ),
        },
    ]


def build_multi_section_messages(
    *,
    spec_id: str,
    sections: list[tuple[str, str]],  # [(clause, section_title), ...]
    excerpt: str,
) -> list[dict[str, str]]:
    """multi_section：给同一 spec 的多段，出"需合并多小节"的题（不跨 spec，见需求 §1.4）。"""
    sec_list = "; ".join(f"{c} {t}".strip() for c, t in sections) or "(n/a)"
    sys = _POSITIVE_SYSTEM.format(categories="multi_section", spec_id=spec_id)
    user = (
        f"Spec: TS {spec_id}  (3GPP)\n"
        f"Clauses involved: {sec_list}\n"
        f"Combined excerpt (multiple sections of the SAME spec):\n"
        f'"""\n{excerpt}\n"""\n\n'
        f"Write ONE multi_section question that REQUIRES combining facts from "
        f"these sections of TS {spec_id} (do not cross into other specs). "
        f"Set expected_specs[0].sections to the involved clauses."
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


_FALSE_PREMISE_SYSTEM = """You are a 3GPP standards expert building the *negative* \
(hallucination-probe) part of a RAG evaluation set. You invent ONE question that \
sounds like a legitimate 3GPP question but is built on a FALSE PREMISE: a concept, \
information element, field, timer, or mechanism that does NOT exist in 3GPP specs.

The goal: a faithful RAG system must REFUSE / say it is not specified, instead of \
fabricating an answer. So the fake premise must be plausible enough to tempt \
fabrication, yet genuinely non-existent.

Hard rules:
- The fake thing must NOT exist in any 3GPP spec. Do not use a real IE/timer/concept.
- The question must read naturally (as if a real user asked it), embedding the fake \
premise as if it were real.
- Stay in the given telecom domain so it is on-topic but still false.

Return a SINGLE JSON object (no markdown fence) with keys:
- "question": the false-premise question.
- "category": "negative".
- "expected_specs": [].
- "expected_facts": [].
- "forbidden": 2-4 SHORT strings naming the fabricated specifics a hallucinating \
answer would invent (e.g. the fake field's "format", "length", "default value"); \
used to catch partial hallucination.
- "probe_terms": 2-4 DISTINCTIVE strings naming the fake concept itself — its full \
invented name + acronym (e.g. "PDU Session Anchor Mobility Trigger", "PSAMT"). Avoid \
plain dictionary words; use the multi-word coined name so it is genuinely absent from \
the corpus. Used to verify the concept does not exist.
- "must_say_not_found": true.
- "language": "en".
- "notes": 1 short sentence explaining WHY the premise is false (what really exists \
instead)."""


_FALSE_PREMISE_USER = """Telecom domain (for on-topic flavor only — do NOT reuse any \
real term from it as the fake premise): {domain}

Invent ONE plausible-but-false 3GPP question in this domain."""


def build_false_premise_messages(*, domain: str) -> list[dict[str, str]]:
    """negative · 不存在的概念/字段（凭空编造型幻觉探针）。"""
    return [
        {"role": "system", "content": _FALSE_PREMISE_SYSTEM},
        {"role": "user", "content": _FALSE_PREMISE_USER.format(domain=domain)},
    ]


_OUT_OF_SCOPE_SYSTEM = """You are a telecom standards expert building the *negative* \
(out-of-scope) part of a RAG evaluation set for a 3GPP-only retrieval system. You write \
ONE question about a REAL networking/telecom topic that is NOT specified by 3GPP — it \
belongs to a DIFFERENT standards body or vendor domain (IEEE 802.11/802.1/802.3, IETF \
RFCs for BGP/OSPF/IS-IS/MPLS, ITU-T PON, DOCSIS, CableLabs, MEF, vendor-proprietary, \
etc.), phrased as if a user wrongly expected 3GPP to define it.

The goal: both systems retrieve ONLY from a 3GPP corpus, which does not contain this \
content. A faithful RAG must say it is outside 3GPP scope / not found in the specs, \
instead of answering from the model's parametric memory. This is symmetric: neither \
system's 3GPP library has it.

Hard rules:
- The topic must be REAL but clearly OWNED BY A NON-3GPP body. Do NOT pick anything \
3GPP itself specifies or normatively profiles (avoid NAS/RRC/NGAP/PFCP/Diameter/GTP, \
5G/LTE core & RAN, 3GPP codecs). Do NOT invent a fake concept (that is the other type).
- Ask for a concrete non-3GPP specific (a field/value/algorithm/procedure) that lives \
in that other standard, e.g. an IEEE 802.11be EHT MCS table entry, a BGP route-reflector \
attribute, an XGS-PON framing field.
- The question must read naturally.

Return a SINGLE JSON object (no markdown fence) with keys:
- "question": the out-of-3GPP-scope question.
- "category": "negative".
- "expected_specs": [].
- "expected_facts": [].
- "forbidden": 2-4 SHORT strings of non-3GPP specifics a hallucinating answer would \
assert.
- "probe_terms": 2-4 DISTINCTIVE identifiers of the owning standard/topic — its \
standard number or signature name (e.g. "802.11be", "RFC 4456", "XGS-PON", "SRv6"). \
Do NOT use generic networking words that 3GPP also uses (e.g. "path attribute", \
"MaxAge", "label", "frame"); only terms that would appear ONLY in the non-3GPP \
standard. Used to verify the topic is absent from the 3GPP corpus.
- "must_say_not_found": true.
- "language": "en".
- "notes": 1 short sentence: which body owns it / why 3GPP does not specify it."""


_OUT_OF_SCOPE_USER = """Non-3GPP standards area to probe: {area}

Write ONE real but out-of-3GPP-scope question in this area."""


def build_out_of_scope_messages(*, area: str) -> list[dict[str, str]]:
    """negative · 域外真实内容（非 3GPP，测 RAG 是否守"只答 3GPP 检索到的"）。"""
    return [
        {"role": "system", "content": _OUT_OF_SCOPE_SYSTEM},
        {"role": "user", "content": _OUT_OF_SCOPE_USER.format(area=area)},
    ]


__all__ = [
    "CHUNK_TYPE_CATEGORIES",
    "POSITIVE_CATEGORIES",
    "build_false_premise_messages",
    "build_multi_section_messages",
    "build_out_of_scope_messages",
    "build_positive_messages",
]
