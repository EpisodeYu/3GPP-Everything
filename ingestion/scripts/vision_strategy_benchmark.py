"""Vision 策略 benchmark：客观对比三种 figure 描述策略。

用户问题（2026-05-15）：
- GSMA 自带描述的质量到底怎么样？
- 比起 mimo-v2.5 的 vision 质量如何？
- 方案 Y（GSMA 描述喂给 mimo 得到结构化输出）能否超过两者各自？

本脚本对 12 张跨类型 figure 跑三种生成方式，输出客观对比报告：

- **方案 A（GSMA only）**：直接用 GSMA raw.md 自带描述（image_alt + caption_text + spec_caption），
  不调任何 vision API
- **方案 B（mimo-v2.5 only）**：现行 hf-vision-smoke 的 prompt（free-text，无上下文）
- **方案 C（plan 方案 Y）**：mimo-v2.5 + GSMA 描述当 caption + 改进 prompt 输出结构化 JSON
  （含 visible_labels / visible_acronyms / figure_kind / spec_role / description）

量化指标：
- description 长度（chars / Voyage tokens）
- visible_labels / visible_acronyms 数量（仅 C 提供结构化）
- 共享术语（acronym）覆盖率：A/B 描述中包含的 5G 标准缩略词数（粗略统计）
- C 的 JSON 解析成功率
- API 调用成本（completion + reasoning tokens）

LLM-as-judge：
- 用 mimo-v2.5 给三组描述按 5 个维度打分（accuracy / coverage / no-hallucination / specificity / brevity）
  注意：mimo 给自己的输出（B/C）评分有 bias，仅作参考。

输出：eval-results/source-audit/vision_strategy_benchmark.md

成本估算：12 图 × (B + C 两次 vision 调用) ≈ 24 次 + judge 12 次 = 36 次 mimo-v2.5 调用。
按 vision_smoke 平均 ~600 ct/次，约 22k tokens 总成本，远低于 §5.2 阈值。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, "/home/s1yu/3GPP-Everything")

from ingestion.chunker.atomic_blocks import parse_atomic_blocks
from ingestion.chunker.figure import FigureExtract, extract_figure
from ingestion.chunker.tokenize_utils import count_tokens
from ingestion.hf_loader import GsmaHfLoader, dedupe_keep_latest, resolve_image
from ingestion.hf_loader.manifest_store import get_meta, manifest_session, read_entries

# ----------- 样本池：12 张跨 8 类 -----------

# (kind_label, spec_id, clause, image_basename)
# clause 用 None 表示找该 spec 的第一个匹配 image_basename 的 figure
SAMPLES: list[tuple[str, str, str | None, str]] = [
    ("logo", "38.211", None, "64662465bba247703fdec49c8f3309f9_img.jpg"),
    ("structure", "23.003", "2.2", "a51105b2031bad93b818b82f071c6add_img.jpg"),
    ("architecture-sbi", "23.501", "4.2.3", "78d5774278a3f4a614f8c0ae485ce8d9_img.jpg"),
    ("architecture-rp", "23.501", "4.2.3", "c0e88e4bd3a209b66ee7cb67e1cec2be_img.jpg"),
    ("message-flow-short", "38.413", "8.2.1.2", "f0b7abcb093621bb310bf61fbe0f0d2d_img.jpg"),
    ("message-flow-long", "23.502", "4.2.2.2.1", "2837ffdadcdb1e5bababa56b564e56ed_img.jpg"),
    ("chart", "36.102", "6.5", "5e16d3613b74558acc74ff6d7fd75fa9_img.jpg"),
    ("crypto-flow", "33.105", "5.1.1.2", "b28af4985cdef1e519e3aaf26561dcb3_img.jpg"),
    ("state-diagram", "24.501", "5.1.3.2.1.1", "de58a0d9b3e49f3279658d910bad1770_img.jpg"),
    ("amr-architecture", "26.071", None, "997233d405f0d4b89ddeb7683e047f66_img.jpg"),
    ("classification", "32.103", "4.2.2", "81a4cbf0b3c4cbc065efdf8f800dadde_img.jpg"),
    ("bit-format", "23.003", None, "09955ff8214ffb6947951fc0f60eb6ab_img.jpg"),
]

# ----- Prompt v2（应用 §4.2 改进清单 7 条；2026-05-15 用户反馈后升级）-----
#
# 关键改动 vs v1：
# - (1) anti-hallucination 边界：禁止补充图中读不到的 3GPP 知识，弱断言
# - (3) 自适应长度：不设句数上限，按图复杂度 / 参照 GSMA 描述深度，"质量优先，
#       宁可长也不要漏"
# - (4) 字面 token 保留：每个 acronym / identifier / function name verbatim
# - (5) 强制枚举 visible_labels：箭头标签 / 坐标轴 / 图例项一个不漏
# - (B 也升级：free-text 但仍应用 1, 3, 4, 5，不变 prompt 输出形式)
# - (C 同时应用 2, 6, 7：caption + surrounding 注入、JSON 输出、undescribable 兜底)
#
# 自适应长度的设计：
# - 不限制句数（用户明确要求"不要过于限制 mimo 的发挥"）
# - 给软指引：参照 GSMA-style depth（median 281 tokens，max ~1000 tokens）
# - max_tokens=8192 是硬上限，但 mimo 按需停止，不会填满

PROMPT_B_FREE_TEXT = """You are reading a figure extracted from a 3GPP technical specification.

Describe what is visible in the figure. Your description must:
- Cover every entity, box, arrow, label, axis name, legend item that is actually
  visible. List each verbatim—do NOT paraphrase acronyms (e.g., write 'AMF' not
  'Access and Mobility Management Function' unless the figure spells it out).
- Preserve every acronym, identifier, function name (e.g., AMR, TMGI, MAC-S, f1*,
  N1, AMF, NSSAAF, Nudm_SDM_Get) exactly as they appear.
- For any inference beyond what is visible (e.g., the spec role, what the figure
  is "for"), use weak assertions: 'likely', 'appears to', 'probably represents'.
  Never state 3GPP domain knowledge that is not visible in the figure as fact.
- DO NOT invent labels or acronyms that are not actually visible. If a label
  is truncated or unreadable, omit it; do NOT guess.
- If the figure is undescribable (corrupted, blank, pure decorative logo with no
  technical content), say so explicitly in 1 sentence.

Length guidance (quality over brevity):
- Match the figure's information density. A simple logo or trivial bit-format
  diagram needs only 1-2 sentences. A dense 5G architecture diagram or a long
  registration message flow needs multiple paragraphs—do not truncate.
- Aim for the depth of a careful human caption written by the spec author.
  Comparable depth to the original GSMA-rendered captions (typically 100-1000
  tokens depending on figure complexity) is a good target.
- Do not pad with generic boilerplate. Every sentence should add visible
  information.

Output plain text only (no JSON, no markdown fences)."""

PROMPT_C_STRUCTURED_TEMPLATE = """You are reading a figure extracted from a 3GPP technical specification.
You are given context that may help you anchor the figure correctly.

Spec: {spec_id} clause {clause} - {section_title}
Existing caption: {spec_caption}
Existing upstream description (from a prior OCR/vision pipeline; treat as a hint
only, may be incomplete or contain errors—correct any errors you can verify
against the figure): {gsma_context}
Surrounding paragraph from spec body: {surrounding_paragraph}

Output STRICT JSON (no prose, no markdown fences):

{{
  "figure_kind": "<one of: logo|architecture|message_flow|state_diagram|block_diagram|chart|formula|bit_format|classification|other|undescribable>",
  "visible_labels": ["<every text label / box title / arrow label / axis name / legend item visible, verbatim>"],
  "visible_acronyms": ["<every 3GPP acronym / identifier / function name visible (e.g., AMF, SMF, UPF, N1, AMR, TMGI, MAC-S, f1*, Nudm_SDM_Get), verbatim, deduplicated>"],
  "description": "<see length guidance below>",
  "spec_role": "<short phrase, e.g., 'reference architecture', 'registration message flow', 'state machine for 5GMM', 'authentication function definition'>",
  "undescribable_reason": "<set only when figure_kind=='undescribable'; explain why (corrupted/blank/illegible). Otherwise empty string.>"
}}

Description length guidance (quality over brevity):
- Match the figure's information density. 1-2 sentences for logos / trivial bit
  formats; multiple paragraphs for dense architecture diagrams or long message
  flows. Do not truncate.
- Aim for the depth of a careful human caption by the spec author. The original
  GSMA upstream descriptions (typically 100-1000 tokens depending on figure
  complexity) are a good reference point for target depth.
- Do not pad with generic boilerplate. Every sentence must add visible
  information.

Strict rules:
- DO NOT invent labels or acronyms that are not actually visible in the figure.
  If a label is truncated or unreadable, omit it; do NOT guess.
- Preserve every acronym / identifier / function name verbatim. Do NOT expand
  acronyms unless the figure itself spells them out.
- For inferences beyond visible content (figure_kind, spec_role, why the figure
  exists), use weak assertions: 'likely', 'appears to', 'probably represents'.
  NEVER state 3GPP domain knowledge as fact unless it is visible in the figure
  or explicitly given in the surrounding paragraph.
- If the upstream description contradicts the figure, trust the figure—correct
  the description.
- If the figure is undescribable (corrupted, blank, pure decorative logo with no
  technical info), set figure_kind="undescribable" and explain in
  undescribable_reason. Description in this case should be 1 sentence.
- Output ONLY the JSON object. No surrounding text, no markdown fences."""

JUDGE_PROMPT_TEMPLATE = """You are evaluating three descriptions of the same 3GPP figure. You also see the figure itself.

Score each description 0-5 on these dimensions (5 = best):
- accuracy: facts in the description match what is visible
- coverage: how many of the visible labels/entities/relationships are mentioned
- no_hallucination: 5 = no false claims; 0 = many false claims
- specificity: prefers concrete acronyms/identifiers over generic phrases
- brevity_balance: 5 = appropriately concise without losing key info; 0 = too long padded OR too short losing info

Spec: {spec_id} clause {clause} - {section_title}

Description A (GSMA upstream):
{desc_a}

Description B (mimo-v2.5 free-text, no context):
{desc_b}

Description C (mimo-v2.5 structured JSON description field, with GSMA as caption):
{desc_c}

Output STRICT JSON (no prose, no markdown fences):

{{
  "A": {{"accuracy": 0-5, "coverage": 0-5, "no_hallucination": 0-5, "specificity": 0-5, "brevity_balance": 0-5}},
  "B": {{...}},
  "C": {{...}},
  "overall_winner": "A" | "B" | "C" | "tie",
  "comments": "<one short paragraph explaining your reasoning>"
}}"""

ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9-]{1,15}\*?\b")
COMMON_NON_ACRONYMS = {
    "A", "AN", "BE", "BY", "DO", "GO", "HE", "IF", "IN", "IS", "IT", "MY", "NO", "OF",
    "ON", "OR", "SO", "TO", "UP", "US", "WE", "AM", "AS", "AT", "ALL", "AND", "ARE",
    "BUT", "FOR", "HAS", "HER", "HIS", "HOW", "ITS", "MAY", "NOT", "OUR", "OUT", "SHE",
    "THE", "WAS", "WHO", "WHY", "YOU", "BEEN", "CAN", "ONE", "TWO", "THREE", "FOUR",
    "FIVE", "SIX", "SEVEN", "EIGHT", "NINE", "TEN", "I", "II", "III", "IV", "V", "VI",
    "JSON", "OK", "URL", "URI", "ID", "IDS",
}


@dataclass(slots=True)
class FigureSample:
    kind_label: str
    spec_id: str
    clause: str | None
    image_basename: str
    image_path: str
    section_title: str
    extract: FigureExtract
    image_bytes: bytes
    image_sha256: str
    surrounding_paragraph: str  # figure 前一个 paragraph block（plan §4.2 改进 #2）


@dataclass(slots=True)
class GenResult:
    method: str  # "A" / "B" / "C"
    description: str
    raw_response: dict | None
    elapsed_s: float
    completion_tokens: int | None
    reasoning_tokens: int | None
    json_parse_ok: bool | None  # only for C
    structured: dict | None  # only for C if parse ok


@dataclass(slots=True)
class JudgeResult:
    scores: dict  # {"A": {...}, "B": {...}, "C": {...}, "overall_winner": ..., "comments": ...}
    elapsed_s: float
    completion_tokens: int | None


# ----------- 数据加载 -----------


def load_samples() -> list[FigureSample]:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    manifest_path = Path(base) / "markdown" / "gsma_manifest.sqlite"
    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")

    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)
    by_spec: dict[str, list] = {}
    for kind, spec_id, clause, basename in SAMPLES:
        by_spec.setdefault(spec_id, []).append((kind, clause, basename))

    samples: list[FigureSample] = []
    for spec_id, items in by_spec.items():
        cands = [e for e in entries if e.spec_id == spec_id]
        if not cands:
            print(f"  WARN: spec {spec_id} not in manifest")
            continue
        entry = dedupe_keep_latest(cands)[0]
        # 同 spec 目录前缀（marked/Rel-19/38_series/38211）
        spec_dir = entry.raw_md_path.rsplit("/", 1)[0]
        for bundle in loader.iter_specs([entry]):
            for sec in bundle.sections:
                blocks = parse_atomic_blocks(sec.body)
                for blk_idx, blk in enumerate(blocks):
                    if blk.kind != "figure":
                        continue
                    ext = extract_figure(blk)
                    if ext is None:
                        continue
                    bn = ext.image_path.rsplit("/", 1)[-1]
                    for kind, clause, target_bn in list(items):
                        if bn != target_bn:
                            continue
                        if clause is not None and sec.clause != clause:
                            continue
                        # 构造 HF repo 全路径：markdown 里是相对路径，需拼上 spec dir
                        repo_image_path = (
                            ext.image_path
                            if ext.image_path.startswith("marked/")
                            else f"{spec_dir}/{bn}"
                        )
                        # plan §4.2 改进 #2：取 figure 前最近的 paragraph 作 surrounding
                        surrounding = ""
                        for back in range(blk_idx - 1, -1, -1):
                            if blocks[back].kind == "paragraph":
                                t = blocks[back].text.strip()
                                if t:
                                    surrounding = t[:800]
                                    break
                        try:
                            img = resolve_image(
                                repo_image_path,
                                revision=revision,
                                token=os.environ.get("HF_TOKEN"),
                            )
                            data = img.local_path.read_bytes()
                        except Exception as exc:
                            print(f"  ERR: resolve {repo_image_path} failed: {exc}")
                            continue
                        samples.append(
                            FigureSample(
                                kind_label=kind,
                                spec_id=spec_id,
                                clause=sec.clause,
                                image_basename=bn,
                                image_path=repo_image_path,
                                section_title=sec.section_title,
                                extract=ext,
                                image_bytes=data,
                                image_sha256=img.sha256,
                                surrounding_paragraph=surrounding,
                            )
                        )
                        items.remove((kind, clause, target_bn))
                        if not items:
                            break
                if not items:
                    break
            break
        if items:
            print(f"  WARN: not found in {spec_id}: {items}")

    # 保持 SAMPLES 原顺序
    order = {(spec_id, basename): i for i, (_, spec_id, _, basename) in enumerate(SAMPLES)}
    samples.sort(key=lambda s: order.get((s.spec_id, s.image_basename), 999))
    return samples


# ----------- 三种生成方式 -----------


def gen_a_gsma(sample: FigureSample) -> GenResult:
    """方案 A：直接拼装 GSMA 自带描述（无 vision API）。"""
    ext = sample.extract
    parts: list[str] = []
    if ext.spec_caption:
        parts.append(f"Caption: {ext.spec_caption}")
    if ext.image_alt:
        parts.append(f"Alt: {ext.image_alt}")
    if ext.gsma_caption_text:
        parts.append(ext.gsma_caption_text)
    desc = "\n".join(p for p in parts if p) or "(empty)"
    return GenResult(
        method="A",
        description=desc,
        raw_response=None,
        elapsed_s=0.0,
        completion_tokens=0,
        reasoning_tokens=None,
        json_parse_ok=None,
        structured=None,
    )


def _post_chat(client: httpx.Client, *, base_url: str, api_key: str, body: dict) -> dict:
    resp = client.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


def gen_b_mimo_freetext(
    client: httpx.Client,
    sample: FigureSample,
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> GenResult:
    b64 = base64.b64encode(sample.image_bytes).decode("ascii")
    t0 = time.time()
    payload = _post_chat(
        client,
        base_url=base_url,
        api_key=api_key,
        body={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT_B_FREE_TEXT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
        },
    )
    elapsed = time.time() - t0
    choice = payload["choices"][0]
    msg = choice["message"]
    desc = (msg.get("content") or "").strip()
    usage = payload.get("usage") or {}
    return GenResult(
        method="B",
        description=desc,
        raw_response=payload,
        elapsed_s=elapsed,
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        json_parse_ok=None,
        structured=None,
    )


def gen_c_mimo_structured(
    client: httpx.Client,
    sample: FigureSample,
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> GenResult:
    ext = sample.extract
    gsma_context = (ext.gsma_caption_text or "").strip()
    if not gsma_context and ext.image_alt:
        gsma_context = ext.image_alt
    if not gsma_context:
        gsma_context = "(none)"

    prompt = PROMPT_C_STRUCTURED_TEMPLATE.format(
        spec_id=sample.spec_id,
        clause=sample.clause or "",
        section_title=sample.section_title,
        spec_caption=ext.spec_caption or "(none)",
        gsma_context=gsma_context,
        surrounding_paragraph=sample.surrounding_paragraph or "(none)",
    )
    b64 = base64.b64encode(sample.image_bytes).decode("ascii")
    t0 = time.time()
    payload = _post_chat(
        client,
        base_url=base_url,
        api_key=api_key,
        body={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
        },
    )
    elapsed = time.time() - t0
    choice = payload["choices"][0]
    msg = choice["message"]
    raw = (msg.get("content") or "").strip()
    usage = payload.get("usage") or {}

    structured = _try_parse_json(raw)
    json_ok = structured is not None and isinstance(structured, dict)
    desc = ""
    if json_ok and "description" in structured:
        desc = str(structured.get("description") or "")
    if not desc:
        desc = raw

    return GenResult(
        method="C",
        description=desc.strip(),
        raw_response=payload,
        elapsed_s=elapsed,
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        json_parse_ok=json_ok,
        structured=structured if json_ok else None,
    )


def _try_parse_json(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        # 去掉 ``` ... ``` 围栏
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 尝试提取第一个 { ... } 块
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ----------- LLM-as-judge -----------


def gen_judge(
    client: httpx.Client,
    sample: FigureSample,
    *,
    desc_a: str,
    desc_b: str,
    desc_c: str,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> JudgeResult | None:
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        spec_id=sample.spec_id,
        clause=sample.clause or "",
        section_title=sample.section_title,
        desc_a=desc_a or "(empty)",
        desc_b=desc_b or "(empty)",
        desc_c=desc_c or "(empty)",
    )
    b64 = base64.b64encode(sample.image_bytes).decode("ascii")
    t0 = time.time()
    try:
        payload = _post_chat(
            client,
            base_url=base_url,
            api_key=api_key,
            body={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                        ],
                    }
                ],
            },
        )
    except Exception as exc:
        print(f"  judge FAIL for {sample.spec_id}/{sample.image_basename}: {exc}")
        return None
    elapsed = time.time() - t0
    raw = (payload["choices"][0]["message"].get("content") or "").strip()
    parsed = _try_parse_json(raw)
    if parsed is None:
        print(f"  judge JSON parse FAIL for {sample.spec_id}/{sample.image_basename}")
        return None
    usage = payload.get("usage") or {}
    return JudgeResult(
        scores=parsed,
        elapsed_s=elapsed,
        completion_tokens=usage.get("completion_tokens"),
    )


# ----------- 量化指标 -----------


def acronyms_in(text: str) -> set[str]:
    if not text:
        return set()
    found = set(ACRONYM_RE.findall(text))
    return {a for a in found if a not in COMMON_NON_ACRONYMS and len(a) >= 2}


def desc_metrics(desc: str) -> dict:
    return {
        "chars": len(desc),
        "tokens": count_tokens(desc),
        "acronyms": sorted(acronyms_in(desc)),
        "acronym_count": len(acronyms_in(desc)),
    }


# ----------- 报告 -----------


def render_report(
    samples: list[FigureSample],
    results: list[dict],
    *,
    model: str,
    out_path: Path,
) -> None:
    lines: list[str] = []
    suffix = os.environ.get("BENCHMARK_VERSION", "v2")
    lines.append(f"# Vision 策略 benchmark {suffix}：A vs B vs C")
    lines.append("")
    lines.append("- benchmark date: 2026-05-15")
    lines.append(f"- vision model: `{model}` (via LiteLLM proxy)")
    lines.append(f"- samples: {len(samples)} figures across {len({s.kind_label for s in samples})} kinds")
    lines.append(
        "- prompt 版本：v2 = 应用 docs §4.2 改进清单 7 条（anti-hallucination / "
        "verbatim acronyms / 强制枚举 visible labels / 自适应长度 / "
        "JSON 输出 / undescribable 兜底 / caption + surrounding 注入）"
    )
    lines.append("")
    lines.append("- 三种方案：")
    lines.append("  - **A** = GSMA 自带描述（image_alt + caption_text + spec_caption），无 API 调用")
    lines.append(
        "  - **B** = mimo-v2.5 free-text + v2 prompt（无 GSMA 上下文）"
        "—— 公平 baseline：升级到与 C 同等 prompt 质量，但不含 GSMA caption / surrounding"
    )
    lines.append(
        "  - **C** = mimo-v2.5 + v2 prompt + GSMA caption + surrounding paragraph 注入 "
        "→ 结构化 JSON（含 visible_labels / visible_acronyms / figure_kind / "
        "spec_role / description / undescribable_reason）"
    )
    lines.append("")
    lines.append("## 1. 总体量化对比")
    lines.append("")

    # 汇总：平均 token / chars / acronym 数 / json 成功率 / 调用成本
    agg: dict[str, dict] = {"A": {}, "B": {}, "C": {}}
    for method in ("A", "B", "C"):
        toks = []
        chars = []
        acrs = []
        elapsed = []
        ct = []
        rt = []
        for r in results:
            g = r["gens"][method]
            m = desc_metrics(g.description)
            toks.append(m["tokens"])
            chars.append(m["chars"])
            acrs.append(m["acronym_count"])
            if method != "A":
                elapsed.append(g.elapsed_s)
                if g.completion_tokens is not None:
                    ct.append(g.completion_tokens)
                if g.reasoning_tokens is not None:
                    rt.append(g.reasoning_tokens)
        agg[method] = {
            "median_tokens": sorted(toks)[len(toks) // 2] if toks else 0,
            "median_chars": sorted(chars)[len(chars) // 2] if chars else 0,
            "mean_acronyms": sum(acrs) / len(acrs) if acrs else 0,
            "max_acronyms": max(acrs) if acrs else 0,
            "median_elapsed_s": sorted(elapsed)[len(elapsed) // 2] if elapsed else 0,
            "median_completion_tokens": sorted(ct)[len(ct) // 2] if ct else 0,
            "median_reasoning_tokens": sorted(rt)[len(rt) // 2] if rt else 0,
        }

    lines.append("| 指标 | A (GSMA) | B (mimo free-text) | C (mimo structured + GSMA) |")
    lines.append("|------|---------:|-------------------:|---------------------------:|")
    lines.append(
        f"| description median tokens | {agg['A']['median_tokens']} | {agg['B']['median_tokens']} | {agg['C']['median_tokens']} |"
    )
    lines.append(
        f"| description median chars | {agg['A']['median_chars']} | {agg['B']['median_chars']} | {agg['C']['median_chars']} |"
    )
    lines.append(
        f"| acronyms 平均数 | {agg['A']['mean_acronyms']:.1f} | {agg['B']['mean_acronyms']:.1f} | {agg['C']['mean_acronyms']:.1f} |"
    )
    lines.append(
        f"| acronyms 最大数 | {agg['A']['max_acronyms']} | {agg['B']['max_acronyms']} | {agg['C']['max_acronyms']} |"
    )
    lines.append(
        f"| API median elapsed s | - | {agg['B']['median_elapsed_s']:.1f} | {agg['C']['median_elapsed_s']:.1f} |"
    )
    lines.append(
        f"| API median completion tokens | - | {agg['B']['median_completion_tokens']} | {agg['C']['median_completion_tokens']} |"
    )
    lines.append(
        f"| API median reasoning tokens | - | {agg['B']['median_reasoning_tokens']} | {agg['C']['median_reasoning_tokens']} |"
    )

    # JSON 解析成功率
    c_ok = sum(1 for r in results if r["gens"]["C"].json_parse_ok)
    lines.append(f"| C JSON 解析成功率 | - | - | **{c_ok}/{len(results)}** |")
    lines.append("")

    # judge 汇总
    judge_results = [r["judge"] for r in results if r["judge"] is not None]
    if judge_results:
        lines.append("## 2. LLM-as-judge 汇总（mimo-v2.5 自评，有 bias）")
        lines.append("")
        dims = ["accuracy", "coverage", "no_hallucination", "specificity", "brevity_balance"]
        lines.append("| 维度 | A | B | C |")
        lines.append("|------|---:|---:|---:|")
        for dim in dims:
            avg = {}
            for m in ("A", "B", "C"):
                vals = [
                    j.scores.get(m, {}).get(dim)
                    for j in judge_results
                    if isinstance(j.scores.get(m), dict) and j.scores.get(m, {}).get(dim) is not None
                ]
                avg[m] = sum(vals) / len(vals) if vals else 0
            lines.append(f"| {dim} | {avg['A']:.2f} | {avg['B']:.2f} | {avg['C']:.2f} |")
        # winner
        winners = [j.scores.get("overall_winner", "?") for j in judge_results]
        win_counts = {"A": 0, "B": 0, "C": 0, "tie": 0, "other": 0}
        for w in winners:
            win_counts[w if w in win_counts else "other"] += 1
        lines.append("")
        lines.append(
            f"**winner 投票（n={len(judge_results)}）**："
            f"A={win_counts['A']} · B={win_counts['B']} · C={win_counts['C']} · tie={win_counts['tie']}"
        )
        lines.append("")

    # 总成本估算
    total_b_ct = sum((r["gens"]["B"].completion_tokens or 0) for r in results)
    total_c_ct = sum((r["gens"]["C"].completion_tokens or 0) for r in results)
    total_judge_ct = sum((r["judge"].completion_tokens or 0) for r in results if r["judge"])
    lines.append("## 3. 成本核算")
    lines.append("")
    lines.append(f"- B 总 completion_tokens（含 reasoning）：{total_b_ct}")
    lines.append(f"- C 总 completion_tokens：{total_c_ct}")
    lines.append(f"- judge 总 completion_tokens：{total_judge_ct}")
    lines.append(
        f"- 全 benchmark 总 completion_tokens：{total_b_ct + total_c_ct + total_judge_ct}"
        f"（{len(results)} 张图）"
    )
    lines.append("")

    # 单图详细对比
    lines.append("## 4. 单图详细对比")
    lines.append("")
    for r in results:
        s: FigureSample = r["sample"]
        ga: GenResult = r["gens"]["A"]
        gb: GenResult = r["gens"]["B"]
        gc: GenResult = r["gens"]["C"]
        lines.append(f"### [{s.kind_label}] {s.spec_id} clause {s.clause or '-'} — `{s.image_basename}`")
        lines.append("")
        lines.append(f"- section_title: {s.section_title}")
        lines.append(f"- image: {len(s.image_bytes)} bytes, sha256 `{s.image_sha256[:16]}...`")
        lines.append("")
        for tag, g in (("A (GSMA)", ga), ("B (mimo free-text)", gb), ("C (mimo structured)", gc)):
            m = desc_metrics(g.description)
            extras = []
            if g.completion_tokens is not None:
                extras.append(f"ct={g.completion_tokens}")
            if g.reasoning_tokens is not None:
                extras.append(f"rt={g.reasoning_tokens}")
            if g.elapsed_s:
                extras.append(f"{g.elapsed_s:.1f}s")
            extras_str = " | ".join(extras)
            lines.append(
                f"**{tag}** — tokens={m['tokens']} chars={m['chars']} "
                f"acronyms={m['acronym_count']} {('| ' + extras_str) if extras_str else ''}"
            )
            lines.append("")
            lines.append("```")
            lines.append(g.description[:1500] + ("..." if len(g.description) > 1500 else ""))
            lines.append("```")
            if g.method == "C" and g.structured:
                lines.append("")
                lines.append(f"- structured.figure_kind: `{g.structured.get('figure_kind')}`")
                vl = g.structured.get("visible_labels") or []
                va = g.structured.get("visible_acronyms") or []
                lines.append(f"- structured.visible_labels ({len(vl)}): {vl[:20]}")
                lines.append(f"- structured.visible_acronyms ({len(va)}): {va[:20]}")
                lines.append(f"- structured.spec_role: {g.structured.get('spec_role')}")
            elif g.method == "C" and g.json_parse_ok is False:
                lines.append("- ⚠️ JSON 解析失败")
            lines.append("")
        if r["judge"]:
            j = r["judge"]
            sa = j.scores.get("A", {})
            sb = j.scores.get("B", {})
            sc = j.scores.get("C", {})
            dims = ["accuracy", "coverage", "no_hallucination", "specificity", "brevity_balance"]
            lines.append("**judge scores**:")
            lines.append("")
            lines.append("| 维度 | A | B | C |")
            lines.append("|------|---|---|---|")
            for d in dims:
                lines.append(
                    f"| {d} | {sa.get(d, '-')} | {sb.get(d, '-')} | {sc.get(d, '-')} |"
                )
            lines.append(f"- winner: **{j.scores.get('overall_winner', '?')}**")
            comments = (j.scores.get("comments") or "").strip()
            if comments:
                lines.append(f"- comments: {comments}")
            lines.append("")
        lines.append("---")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ----------- main -----------


def main() -> int:
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    model = os.environ.get("LLM_VISION_MODEL") or "mimo-v2.5"
    if not base_url or not api_key:
        print("FATAL: LITELLM_BASE_URL / LITELLM_API_KEY 必须在 .env 中提供")
        return 1

    # v2: 升级 prompt（应用 §4.2 改进清单 7 条 + 自适应长度）后重跑；保留 v1 对比
    suffix = os.environ.get("BENCHMARK_VERSION", "v2")
    out = Path(
        f"/home/s1yu/3GPP-Everything/eval-results/source-audit/vision_strategy_benchmark_{suffix}.md"
    )

    print(f"[benchmark] loading {len(SAMPLES)} samples ...")
    samples = load_samples()
    print(f"[benchmark] loaded {len(samples)} samples")
    if len(samples) < len(SAMPLES):
        print(f"[benchmark] WARN: missing {len(SAMPLES) - len(samples)} samples")

    results: list[dict] = []
    with httpx.Client(timeout=httpx.Timeout(180.0)) as client:
        for i, s in enumerate(samples, 1):
            print(f"[{i}/{len(samples)}] {s.kind_label} | {s.spec_id} | {s.image_basename}")
            ga = gen_a_gsma(s)
            print(f"  A: chars={len(ga.description)} tokens={count_tokens(ga.description)}")

            print("  B: calling mimo free-text ...")
            try:
                gb = gen_b_mimo_freetext(
                    client, s, base_url=base_url, api_key=api_key, model=model, max_tokens=8192
                )
                print(f"     ct={gb.completion_tokens} rt={gb.reasoning_tokens} elapsed={gb.elapsed_s:.1f}s")
            except Exception as exc:
                print(f"     B FAIL: {exc}")
                gb = GenResult(
                    method="B",
                    description=f"(error: {exc})",
                    raw_response=None,
                    elapsed_s=0,
                    completion_tokens=None,
                    reasoning_tokens=None,
                    json_parse_ok=None,
                    structured=None,
                )

            print("  C: calling mimo structured ...")
            try:
                gc = gen_c_mimo_structured(
                    client,
                    s,
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    max_tokens=8192,
                )
                print(
                    f"     ct={gc.completion_tokens} rt={gc.reasoning_tokens} "
                    f"json_ok={gc.json_parse_ok} elapsed={gc.elapsed_s:.1f}s"
                )
            except Exception as exc:
                print(f"     C FAIL: {exc}")
                gc = GenResult(
                    method="C",
                    description=f"(error: {exc})",
                    raw_response=None,
                    elapsed_s=0,
                    completion_tokens=None,
                    reasoning_tokens=None,
                    json_parse_ok=False,
                    structured=None,
                )

            print("  judge: calling mimo judge ...")
            judge = gen_judge(
                client,
                s,
                desc_a=ga.description,
                desc_b=gb.description,
                desc_c=gc.description,
                base_url=base_url,
                api_key=api_key,
                model=model,
                max_tokens=4096,
            )
            if judge:
                print(
                    f"     winner={judge.scores.get('overall_winner', '?')} "
                    f"ct={judge.completion_tokens} elapsed={judge.elapsed_s:.1f}s"
                )

            results.append(
                {
                    "sample": s,
                    "gens": {"A": ga, "B": gb, "C": gc},
                    "judge": judge,
                }
            )

    print(f"\n[benchmark] writing report → {out}")
    render_report(samples, results, model=model, out_path=out)
    print(f"[benchmark] done. {len(results)} samples processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
