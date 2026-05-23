"""M7.7 — 修复 teleqna_transformed 黄金集"missing 而非能力"导致的丢分。

诊断结论（见 eval-results/m7-post-m75-baseline/report.md §6.3 / §6.4）：

1. **forbidden 错**：transformer 把 MCQ 干扰选项当 forbidden 直接灌入；导致正确
   答案里很多本来合法的术语（如 def-040 的 "EAS address information" 就是问的
   内容本身）被算违规。**对策**：清空所有 teleqna 非 negative 题的 forbidden。
2. **expected_facts 错**：写成完整句而不是可 substring 的短语；含连字符 / 标点
   差异（H-SMF vs H‑SMF）/ 语序差异 → 49/119 题 spec_recall=1 但 fact_coverage<0.5。
   **对策**：用 deepseek-v4-pro 把每条 fact 转成 2-5 个原子关键短语；保留原句
   到 notes 便于追溯。

用法（在仓库根目录跑）：

    uv run --project eval python -m eval.scripts.golden_repair_teleqna \\
        --golden eval/golden/v1.yaml \\
        --out eval/golden/v1.repaired.yaml \\
        --do-forbidden \\
        --do-facts --facts-budget 200

    # dry-run 看 audit 报告，不写：
    uv run --project eval python -m eval.scripts.golden_repair_teleqna \\
        --golden eval/golden/v1.yaml --audit-only

输出：

- 修复后的 golden（YAML，保留原文件全部字段顺序）
- audit 报告打到 stdout（人审用）
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from eval.settings import EvalSettings

log = logging.getLogger("golden_repair_teleqna")

_REWRITE_SYSTEM_PROMPT = """You rewrite 3GPP test-question "expected_facts" lists.

Input you receive: (question, current_facts).
Each current_fact is a full descriptive sentence. They are used by an evaluation
runner that does case-insensitive **substring** matching against a free-text
answer. Long sentences rarely substring-match because of wording / hyphen /
punctuation differences (e.g. "H-SMF provides VPLMN Specific Offloading Information"
won't match "The H‑SMF provides V-PLMN Specific Offloading Information").

Your task: transform the facts into **atomic key phrases** (2-5 words each,
canonical 3GPP terminology) that any correct free-text answer to this question
would naturally contain verbatim (modulo case).

Rules:
- Output 3-6 atomic phrases total (across all input facts; don't pad).
- Each phrase = a noun phrase / term / number / identifier (e.g. "PDU Session",
  "5G-GUTI", "NF service consumer", "16QAM", "240 kHz").
- DO NOT keep full sentences. DO NOT include articles ("the", "a") at the start.
- DO NOT include trailing punctuation.
- Use canonical 3GPP capitalization (e.g. "gNB-DU", "NG-RAN", "AMF").
- If a fact is a specific identifier or message name, output it verbatim
  (e.g. "INITIAL UL RRC MESSAGE TRANSFER").
- If a fact is metadata (e.g. "Content defined in 3GPP 29.502", "Part of 5G
  Service Based Architecture"), DROP it — these phrases never naturally appear.
- If a fact contains a value (number, version, code rate), keep the value as a
  separate phrase (e.g. "378", "1.4766").
- Output strict JSON: {"facts": ["phrase1", "phrase2", ...]}

Example:

Input:
question: "What information does the H-SMF provide to the V-SMF in the Nsmf_PDUSession_Create Response?"
current_facts:
  - "H-SMF provides VPLMN Specific Offloading Information"
  - "Information is sent to V-SMF in Nsmf_PDUSession_Create Response"
  - "Offloading information is specific to VPLMN"
  - "Content defined in 3GPP 29.502"
  - "Part of H-SMF to V-SMF interaction"

Output:
{"facts": ["VPLMN Specific Offloading Information", "Nsmf_PDUSession_Create", "H-SMF", "V-SMF"]}
"""


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _save_yaml(data: dict[str, Any], path: Path) -> None:
    # preserve unicode + double-quoted strings where needed
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=4096,
        )


def _is_teleqna_non_negative(item: dict[str, Any]) -> bool:
    return (
        item.get("source", "").startswith("teleqna")
        and item.get("category") != "negative"
    )


def _audit(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Survey teleqna golden quality before any change."""
    tq = [it for it in items if it.get("source", "").startswith("teleqna")]
    tq_non_neg = [it for it in tq if it.get("category") != "negative"]
    with_forbidden = [it for it in tq_non_neg if it.get("forbidden")]
    forbidden_total = sum(len(it.get("forbidden") or []) for it in with_forbidden)
    facts_long = []  # facts that look like full sentences (likely substring-unfriendly)
    facts_total = 0
    for it in tq_non_neg:
        for f in it.get("expected_facts") or []:
            facts_total += 1
            if len(f) > 60 or f.endswith(".") or (" " in f and f.count(" ") >= 5):
                facts_long.append((it["id"], f))
    return {
        "teleqna_total": len(tq),
        "teleqna_non_negative": len(tq_non_neg),
        "non_negative_with_forbidden": len(with_forbidden),
        "non_negative_forbidden_entries": forbidden_total,
        "facts_total": facts_total,
        "facts_long_or_sentence_like": len(facts_long),
    }


def repair_forbidden(items: list[dict[str, Any]]) -> int:
    """Clear `forbidden` for all teleqna non-negative items. Return count changed."""
    changed = 0
    for it in items:
        if not _is_teleqna_non_negative(it):
            continue
        fb = it.get("forbidden") or []
        if not fb:
            continue
        original = list(fb)
        it["forbidden"] = []
        # leave a note breadcrumb so the change is auditable from the YAML itself
        note = (
            "M7.7 2026-05-23 cleared MCQ-distractor-derived forbidden list "
            f"(was: {original}); see eval-results/m7-post-m75-baseline/report.md §6.3"
        )
        existing_notes = it.get("notes")
        if existing_notes:
            it["notes"] = f"{existing_notes}\n\n{note}"
        else:
            it["notes"] = note
        changed += 1
    return changed


def _call_litellm_rewrite(
    s: EvalSettings,
    *,
    question: str,
    facts: list[str],
    model: str,
    timeout_s: float = 60.0,
) -> list[str] | None:
    """Single sync HTTP call to litellm; return new facts list or None on error."""
    user_msg = (
        f"question: {question}\ncurrent_facts:\n"
        + "\n".join(f"  - {f}" for f in facts)
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        # deepseek v4 系列是 reasoning 模型；reasoning_content 占完 max_tokens
        # 之后 content 会返回空字符串。给 4000 足以容纳 reasoning + JSON 输出。
        # 实测纯输出 ~50 token，reasoning 一般 < 500 token。
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {s.litellm_api_key}",
        "Content-Type": "application/json",
    }
    url = s.resolved_litellm_base_url.rstrip("/") + "/chat/completions"
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"] or ""
        if not content.strip():
            log.warning("empty content from litellm for question prefix: %.40s", question)
            return None
        import json as _json

        obj = _json.loads(content)
        new_facts = obj.get("facts") or []
        if not isinstance(new_facts, list):
            return None
        # sanitize: strings only, strip whitespace + trailing punctuation
        cleaned: list[str] = []
        for f in new_facts:
            if not isinstance(f, str):
                continue
            f = f.strip().strip(".").strip(",").strip()
            if f:
                cleaned.append(f)
        return cleaned if cleaned else None
    except Exception as exc:
        log.warning("LLM rewrite failed for %.40s: %s", question, exc)
        return None


def repair_facts(
    items: list[dict[str, Any]],
    *,
    settings: EvalSettings,
    model: str,
    budget: int,
) -> tuple[int, int]:
    """Rewrite long-sentence expected_facts via LLM. Return (touched, errors)."""
    touched = 0
    errors = 0
    for it in items:
        if not _is_teleqna_non_negative(it):
            continue
        if touched + errors >= budget:
            log.info("hit budget=%d; stopping rewrite loop", budget)
            break
        cur = it.get("expected_facts") or []
        if not cur:
            continue
        # Heuristic: skip if all facts already look atomic (≤ 4 words, no trailing period)
        atomic = all(
            (not f.endswith(".") and f.count(" ") <= 3) for f in cur if isinstance(f, str)
        )
        if atomic:
            continue
        log.info("[%s] rewriting %d facts", it["id"], len(cur))
        new_facts = _call_litellm_rewrite(
            settings, question=it["question"], facts=cur, model=model
        )
        if not new_facts:
            errors += 1
            continue
        # archive original
        it.setdefault("_original_expected_facts", cur)
        it["expected_facts"] = new_facts
        # leave breadcrumb in notes
        note = (
            f"M7.7 2026-05-23 expected_facts rewritten via {model} (long sentences → atomic phrases);"
            " original archived under `_original_expected_facts`."
        )
        existing_notes = it.get("notes")
        if existing_notes:
            it["notes"] = f"{existing_notes}\n\n{note}"
        else:
            it["notes"] = note
        touched += 1
        time.sleep(0.1)  # be gentle to upstream
    return touched, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None, help="不传 → 默认 {golden}.repaired.yaml")
    ap.add_argument("--audit-only", action="store_true")
    ap.add_argument("--do-forbidden", action="store_true")
    ap.add_argument("--do-facts", action="store_true")
    ap.add_argument(
        "--facts-budget", type=int, default=200, help="最多重写多少题（cost guardrail）"
    )
    ap.add_argument("--model", default="deepseek-v4-pro")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    data = _load_yaml(args.golden)
    items = data.get("items") or []
    if not items:
        log.error("no items found in %s", args.golden)
        return 1
    pre = _audit(items)
    print("=== AUDIT (before repair) ===")
    for k, v in pre.items():
        print(f"  {k}: {v}")
    if args.audit_only:
        return 0
    if not (args.do_forbidden or args.do_facts):
        print("nothing to do (pass --do-forbidden and/or --do-facts)")
        return 0
    if args.do_forbidden:
        n = repair_forbidden(items)
        print(f"[forbidden] cleared on {n} teleqna non-negative items")
    if args.do_facts:
        s = EvalSettings()
        if not s.litellm_api_key:
            print("ERROR: LITELLM_API_KEY missing; cannot call LLM", file=sys.stderr)
            return 2
        touched, errors = repair_facts(
            items, settings=s, model=args.model, budget=args.facts_budget
        )
        print(f"[facts] rewrote: {touched} items, errors: {errors}")
    post = _audit(items)
    print()
    print("=== AUDIT (after repair) ===")
    for k, v in post.items():
        print(f"  {k}: {v}")
    out = args.out or args.golden.with_suffix(".repaired.yaml")
    _save_yaml(data, out)
    print(f"wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
