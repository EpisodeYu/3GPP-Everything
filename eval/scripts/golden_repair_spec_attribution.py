"""M7.7+ — 修复 teleqna_transformed 黄金集 `expected_specs` 归属错误。

诊断：m7-post-m75-baseline 175 题里 64/119 teleqna 题 citations 完全不含
expected_specs。抽样发现是 transformer 程序化逻辑出 bug —— `expected_specs_inferred`
没读 TeleQnA `explanation` 字段（explanation 通常明文写 "defined in TS X.YYY"），
而是按 question 里出现的第一个 spec ID 兜底。

修法：每条 teleqna 题用 mimo-v2.5-pro 看 (question, correct-option, explanation)，
让 LLM 抽取 canonical owner spec_id；与当前 golden 对比：

- 完全一致 → 不动
- LLM 给出新集合且 confidence=high → 自动 fix（替换 expected_specs[*].spec_id）
- confidence=medium / low → 写到 audit 报告，等人 review

原 expected_specs 归档到 `_original_expected_specs` 字段（与 M7.7 facts repair
风格一致）；notes 末尾加 breadcrumb。

用法：

    uv run --project eval python -m eval.scripts.golden_repair_spec_attribution \\
        --golden eval/golden/v1.yaml \\
        --teleqna eval/teleqna/data/filtered/filtered.jsonl \\
        --out eval/golden/v1.yaml \\
        --audit-out eval-results/m7.7-spec-attribution-audit.md \\
        --apply-confidence high \\
        --model mimo-v2.5-pro

dry-run（仅出 audit）：

    uv run --project eval python -m eval.scripts.golden_repair_spec_attribution \\
        --golden eval/golden/v1.yaml \\
        --teleqna eval/teleqna/data/filtered/filtered.jsonl \\
        --audit-out /tmp/audit.md \\
        --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from eval.settings import EvalSettings

log = logging.getLogger("spec_attribution")

_PROMPT_SYSTEM = """You are an expert on 3GPP specifications. Given a TeleQnA
multiple-choice item (question + correct option + explanation), identify the
**canonical 3GPP TS specification(s) that normatively define the correct answer**.

Use BOTH (a) any spec references in the explanation AND (b) your 3GPP
expertise about which TS canonically owns each concept.

Confidence rules:
- **high**: explanation cites the TS verbatim, OR the concept is famously and
  unambiguously in one specific TS (well-known canonical mappings).
- **medium**: inferred via 3GPP knowledge with some ambiguity (e.g., concept
  could plausibly be in 2 specs, or release version uncertain).
- **low**: truly cannot tell which TS owns this; ambiguous concept.

Canonical-owner cheat-sheet (use when relevant; not exhaustive):
- NR RRC procedures / IE definitions / measurement configurations → 38.331
- NR PHY channels / signals (PUSCH, PUCCH, PDSCH, PRACH, SRS structure) → 38.211
- NR PHY procedures (power control, HARQ, BWP timer, scheduling) → 38.213 / 38.214
- NR MAC procedures (RACH procedure, DRX, BWP inactivity timer) → 38.321
- NR NAS / 5GS NAS protocol on N1 → 24.501
- NG-AP (N2 interface) procedures + messages → 38.413
- F1-AP (gNB-DU ↔ gNB-CU) → 38.473
- E1-AP (gNB-CU-CP ↔ gNB-CU-UP) → 38.463
- NG-RAN overall architecture / general descriptions → 38.300 (and 38.401 for split)
- 5GS architecture (NF roles, references) → 23.501
- 5GS procedures (registration, PDU session, handover) → 23.502
- 5GS policy / charging architecture → 23.503
- Service-based interface common: 29.500 / 29.501
- AMF services: 29.518 ; SMF: 29.502 ; PCF: 29.512 ; UDM: 29.503 ; NRF: 29.510 ; UDR: 29.504
- HSS architecture in EPS: 23.002 (canonical); EPS system: 23.401
- SCEF (Service Capability Exposure): 23.682
- LTE PHY channels: 36.211 ; LTE PHY procedures: 36.213 ; LTE RRC: 36.331
- Charging architecture: 32.240 / 32.255 ; OAM: 28.xxx
- Security: 33.501 (5G), 33.401 (LTE), 33.203 (IMS)

Strict output rules:
- spec_ids: 5-digit dotted form ("38.331", "23.501"); multi-part like "38.521-3";
  NO "TS" prefix.
- Prefer 1-2 specs (the most canonical owners). 3+ specs only if the answer
  truly spans them.
- If the question stem cites a spec ("In TS 38.211 ...") AND the answer aligns,
  return that spec.
- Output strict JSON: {"spec_ids": ["XX.YYY", ...], "confidence": "high"|"medium"|"low", "reasoning": "<1 sentence>"}

Examples:

Input:
question: "What identifier is used to uniquely identify the UE within the AMF?"
correct_option: "AMF UE NGAP ID"
explanation: "The AMF UE NGAP ID uniquely identifies the UE within the AMF on the NG-C interface."

Output:
{"spec_ids": ["38.413"], "confidence": "high", "reasoning": "AMF UE NGAP ID is a normative IE defined in TS 38.413 (NG-AP)"}

Input:
question: "What physical channel carries Hybrid ARQ ACK/NAKs for uplink in LTE?"
correct_option: "PHICH"
explanation: "PHICH carries HARQ-ACK feedback for uplink transmissions in LTE."

Output:
{"spec_ids": ["36.211"], "confidence": "high", "reasoning": "PHICH is a physical channel canonically defined in TS 36.211 §6.9"}

Input:
question: "Describe the action on BWP inactivity timer expiry."
correct_option: "Switch the active DL BWP to the default BWP"
explanation: "When the BWP inactivity timer expires, the UE switches to the default BWP."

Output:
{"spec_ids": ["38.321"], "confidence": "high", "reasoning": "BWP inactivity timer behavior is defined in MAC spec TS 38.321"}

Input:
question: "Some vague concept."
correct_option: "..."
explanation: "..." (doesn't help, concept is ambiguous)

Output:
{"spec_ids": [], "confidence": "low", "reasoning": "ambiguous; need more context"}
"""

_SPEC_ID_RE = re.compile(r"^\d{2}\.\d{3}(-\d+)?$")


def _load_yaml(p: Path) -> dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _save_yaml(d: dict[str, Any], p: Path) -> None:
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            d, f, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096
        )


def _load_teleqna(p: Path) -> dict[str, dict[str, Any]]:
    """origin_id -> raw record."""
    out: dict[str, dict[str, Any]] = {}
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rid := r.get("id"):
                out[rid] = r
    return out


def _extract_correct_option(rec: dict[str, Any]) -> str:
    """e.g. 'option 3: TS 23.303' → 'TS 23.303'."""
    ans = rec.get("answer", "")
    if ":" in ans:
        return ans.split(":", 1)[1].strip()
    return ans.strip()


def _normalize_specs(raw: list[Any]) -> list[str]:
    out: list[str] = []
    for s in raw or []:
        s2 = str(s).strip().replace("TS ", "").replace("ts ", "")
        if _SPEC_ID_RE.match(s2):
            out.append(s2)
    return sorted(set(out))


def _current_expected_specs(item: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(es.get("spec_id", "")).strip()
            for es in item.get("expected_specs") or []
            if es.get("spec_id")
        }
    )


def _call_llm(
    s: EvalSettings, *, system: str, user: str, model: str, timeout_s: float = 60.0
) -> dict[str, Any] | None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
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
            return None
        return json.loads(content)
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return None


def attribute_item(
    s: EvalSettings, item: dict[str, Any], tq_rec: dict[str, Any], *, model: str
) -> dict[str, Any] | None:
    correct = _extract_correct_option(tq_rec)
    user = (
        f"question: {tq_rec.get('question', '')}\n"
        f"correct_option: {correct}\n"
        f"explanation: {tq_rec.get('explanation', '')}\n"
    )
    raw = _call_llm(s, system=_PROMPT_SYSTEM, user=user, model=model)
    if not raw:
        return None
    spec_ids = _normalize_specs(raw.get("spec_ids") or [])
    conf = str(raw.get("confidence", "low")).lower()
    if conf not in {"high", "medium", "low"}:
        conf = "low"
    return {
        "spec_ids": spec_ids,
        "confidence": conf,
        "reasoning": str(raw.get("reasoning", ""))[:300],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--teleqna", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None, help="若不传则等于 golden（覆盖）")
    ap.add_argument("--audit-out", required=True, type=Path)
    ap.add_argument(
        "--apply-confidence",
        choices=["high", "medium", "none"],
        default="high",
        help="哪些 confidence 档的 diff 直接 apply（剩下的只进 audit）",
    )
    ap.add_argument("--model", default="mimo-v2.5-pro")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    data = _load_yaml(args.golden)
    items = data.get("items") or []
    tq_data = _load_teleqna(args.teleqna)

    s = EvalSettings()
    if not s.litellm_api_key:
        print("ERROR: LITELLM_API_KEY missing", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    n_called = 0
    for it in items:
        if not it.get("source", "").startswith("teleqna"):
            continue
        if it.get("category") == "negative":
            continue  # negative items: expected_specs intentionally empty
        if n_called >= args.budget:
            log.warning("hit budget=%d", args.budget)
            break
        origin = it.get("teleqna_origin_id")
        tq_rec = tq_data.get(origin or "")
        if not tq_rec:
            results.append(
                {
                    "id": it["id"],
                    "status": "missing_teleqna_record",
                    "current": _current_expected_specs(it),
                }
            )
            continue
        attr = attribute_item(s, it, tq_rec, model=args.model)
        n_called += 1
        if not attr:
            results.append(
                {"id": it["id"], "status": "llm_error", "current": _current_expected_specs(it)}
            )
            continue
        cur = _current_expected_specs(it)
        proposed = attr["spec_ids"]
        same = sorted(cur) == sorted(proposed)
        results.append(
            {
                "id": it["id"],
                "status": "agree" if same else "diff",
                "current": cur,
                "proposed": proposed,
                "confidence": attr["confidence"],
                "reasoning": attr["reasoning"],
                "question": (it.get("question") or "")[:120].replace("\n", " "),
            }
        )
        time.sleep(0.05)

    # Apply high-confidence fixes (if enabled)
    applied = 0
    applied_ids: list[str] = []
    if not args.dry_run and args.apply_confidence != "none":
        threshold = {"high": {"high"}, "medium": {"high", "medium"}}[args.apply_confidence]
        for it in items:
            r = next((x for x in results if x["id"] == it["id"]), None)
            if not r or r["status"] != "diff":
                continue
            if r["confidence"] not in threshold:
                continue
            if not r["proposed"]:
                continue  # never replace with empty list
            old_es = it.get("expected_specs") or []
            it["_original_expected_specs"] = [dict(es) for es in old_es]
            # Preserve sections if old expected_specs[*].spec_id matched a new one
            old_section_map: dict[str, list[str]] = {}
            for es in old_es:
                old_section_map[str(es.get("spec_id", ""))] = [
                    str(s) for s in (es.get("sections") or [])
                ]
            new_es = []
            for sid in r["proposed"]:
                new_es.append({"spec_id": sid, "sections": old_section_map.get(sid, [])})
            it["expected_specs"] = new_es
            note = (
                f"M7.7+ 2026-05-24 expected_specs attribution fixed via {args.model} "
                f"(confidence={r['confidence']}; reasoning: {r['reasoning']}); "
                f"original archived under `_original_expected_specs`."
            )
            existing = it.get("notes")
            it["notes"] = f"{existing}\n\n{note}" if existing else note
            applied += 1
            applied_ids.append(it["id"])

    # Save golden (only if changed)
    out_path = args.out or args.golden
    if not args.dry_run and applied > 0:
        _save_yaml(data, out_path)

    # Audit report
    diff_high = [r for r in results if r.get("status") == "diff" and r.get("confidence") == "high"]
    diff_med = [r for r in results if r.get("status") == "diff" and r.get("confidence") == "medium"]
    diff_low = [r for r in results if r.get("status") == "diff" and r.get("confidence") == "low"]
    agree = [r for r in results if r.get("status") == "agree"]
    errs = [r for r in results if r.get("status") in {"llm_error", "missing_teleqna_record"}]

    lines: list[str] = []
    lines.append("# Spec Attribution Audit (M7.7+, 2026-05-24)")
    lines.append("")
    lines.append(f"- golden: `{args.golden}`")
    lines.append(f"- teleqna raw: `{args.teleqna}`")
    lines.append(f"- model: `{args.model}`")
    lines.append(f"- items scanned: {len(results)}")
    lines.append(f"  - agree (LLM 同意当前): {len(agree)}")
    lines.append(f"  - diff high-conf: {len(diff_high)}")
    lines.append(f"  - diff medium-conf: {len(diff_med)}")
    lines.append(f"  - diff low-conf: {len(diff_low)}")
    lines.append(f"  - errors / missing teleqna_origin: {len(errs)}")
    lines.append(f"- applied (auto-fixed): {applied}")
    lines.append("")

    def _table(title: str, rows: list[dict[str, Any]]) -> None:
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("（无）")
            lines.append("")
            return
        lines.append("| id | current | proposed | confidence | reasoning | question |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                f"| {r['id']} | `{r['current']}` | `{r['proposed']}` | {r['confidence']} | {r['reasoning'][:100]} | {r['question'][:80]} |"
            )
        lines.append("")

    _table("Diff — high confidence (已自动 apply 的部分)", diff_high)
    _table("Diff — medium confidence (人工 review)", diff_med)
    _table("Diff — low confidence (LLM 不确定，建议保留原 spec)", diff_low)
    lines.append("## Errors / missing teleqna records")
    lines.append("")
    for r in errs:
        lines.append(f"- {r['id']}: {r['status']} (current={r['current']})")
    args.audit_out.parent.mkdir(parents=True, exist_ok=True)
    args.audit_out.write_text("\n".join(lines), encoding="utf-8")
    print(f"audit → {args.audit_out}")
    print(
        f"scanned={len(results)} agree={len(agree)} diff_high={len(diff_high)} "
        f"diff_med={len(diff_med)} diff_low={len(diff_low)} errs={len(errs)} "
        f"applied={applied}"
    )
    if applied_ids and args.verbose:
        print("applied items:", applied_ids[:20], "..." if len(applied_ids) > 20 else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
