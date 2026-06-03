"""scores.json → compare_report.md（README §8.3）。

聚合三系统对比:
- **scorecard**:每系统 fact_coverage / 拒答(VALID/INVALID=幻觉率) / spec归属命中 /
  fact-in-context recall(A/B) / 利用率(A/B) / 答题率。
- **3×3 成对胜率矩阵**(位置对冲后)。
- **头条**:RAG(A/B) vs 裸LLM(C) 的 fact_coverage + spec归属,按 spec 冷门度(核心 vs 长尾 series)拆。
- **检索 vs 生成拆解**:recall(检索到) vs coverage(答出来) vs utilization。

用法:
    PYTHONPATH=/data/3GPP-Everything eval/.venv/bin/python -m eval.huawei_compare.compare_report \
        --scores eval-results/huawei-compare/scores.json \
        --out eval-results/huawei-compare/compare_report.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

# 冷门度:核心 series(最常被查) vs 长尾。用 expected_specs[0] 前两位判。
CORE_SERIES: frozenset[str] = frozenset({"23", "38", "29", "24", "33"})


def _mean(vals: list[Any]) -> float | None:
    nums = [v for v in vals if isinstance(v, (int, float))]
    return round(mean(nums), 3) if nums else None


def _pct(part: int, total: int) -> str:
    return f"{part}/{total} ({100 * part // total}%)" if total else "0/0 (–)"


def _series_of(item: dict) -> str:
    specs = item.get("expected_specs") or []
    return specs[0][:2] if specs else "?"


def summarize(scores: dict) -> dict:
    """从 scores.json 聚出报告所需的所有数字（纯函数，可测）。"""
    systems: list[str] = list(scores.get("systems") or [])
    items: list[dict] = scores.get("items") or []
    positives = [it for it in items if it["category"] != "negative"]
    negatives = [it for it in items if it["category"] == "negative"]

    def cell(it: dict, sys: str) -> dict | None:
        c = it["per_system"].get(sys)
        return c if (c and c.get("present")) else None

    per_system: dict[str, dict] = {}
    for sys in systems:
        pos_cells = [c for it in positives if (c := cell(it, sys))]
        neg_cells = [c for it in negatives if (c := cell(it, sys))]
        all_cells = [c for it in items if (c := cell(it, sys))]

        neg_verdicts = [c.get("negative_verdict") for c in neg_cells]
        n_neg = sum(1 for v in neg_verdicts if v is not None)
        per_system[sys] = {
            "answer_rate": _pct(sum(1 for c in all_cells if c.get("ok")), len(items)),
            "fact_coverage": _mean([c.get("fact_coverage") for c in pos_cells]),
            "spec_recall": _pct(
                sum(1 for c in pos_cells if c.get("spec_recall")),
                sum(1 for c in pos_cells if c.get("spec_recall") is not None),
            ),
            "fact_in_context_recall": _mean([c.get("fact_in_context_recall") for c in pos_cells]),
            "utilization": _mean([c.get("utilization") for c in pos_cells]),
            "neg_valid_refusal": _pct(sum(1 for v in neg_verdicts if v == "VALID_REFUSAL"), n_neg),
            "neg_invalid_halluc": _pct(sum(1 for v in neg_verdicts if v == "INVALID"), n_neg),
            "neg_partial": _pct(sum(1 for v in neg_verdicts if v == "PARTIAL_REFUSAL"), n_neg),
        }

    # 头条:fact_coverage / spec-hit 按 核心 vs 长尾
    headline: dict[str, dict] = {}
    for label in ("core", "tail"):
        pos_sub = [it for it in positives if (_series_of(it) in CORE_SERIES) == (label == "core")]
        row: dict[str, dict] = {}
        for sys in systems:
            cells = [c for it in pos_sub if (c := it["per_system"].get(sys)) and c.get("present")]
            row[sys] = {
                "n": len(pos_sub),
                "fact_coverage": _mean([c.get("fact_coverage") for c in cells]),
                "spec_hit": _pct(
                    sum(1 for c in cells if c.get("spec_recall")),
                    sum(1 for c in cells if c.get("spec_recall") is not None),
                ),
            }
        headline[label] = row

    # 成对胜率
    pairwise = scores.get("pairwise") or {}
    pw_summary: dict[str, dict] = {}
    for key, recs in pairwise.items():
        s1, s2 = key.split("_vs_")
        w1 = sum(1 for r in recs if r["winner"] == s1)
        w2 = sum(1 for r in recs if r["winner"] == s2)
        tie = sum(1 for r in recs if r["winner"] == "TIE")
        pw_summary[key] = {"s1": s1, "s2": s2, "w1": w1, "w2": w2, "tie": tie, "n": len(recs)}

    return {
        "systems": systems,
        "n_items": len(items),
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "judge_model": scores.get("judge_model"),
        "per_system": per_system,
        "headline": headline,
        "pairwise": pw_summary,
    }


_SYS_NAME = {"A": "A=本项目RAG", "B": "B=华为RAG", "C": "C=裸LLM"}


def render_markdown(scores: dict) -> str:
    s = summarize(scores)
    sysl = s["systems"]
    L: list[str] = []
    L.append("# 华为对比测试报告（A=本项目 vs B=华为 vs C=裸LLM）\n")
    L.append(
        f"题集 {s['n_items']}（正题 {s['n_positive']} / 负题 {s['n_negative']}）｜"
        f"裁判 `{s['judge_model']}`（与三方都不同源）\n"
    )

    L.append("\n## 1. Scorecard（每系统）\n")
    L.append("| 指标 | " + " | ".join(_SYS_NAME.get(x, x) for x in sysl) + " |")
    L.append("|---|" + "---|" * len(sysl))
    rows = [
        ("fact_coverage（正确性,正题均值）", "fact_coverage"),
        ("spec 归属命中（可溯源,正题）", "spec_recall"),
        ("fact-in-context recall（检索到,A/B）", "fact_in_context_recall"),
        ("利用率（检索后答出,A/B）", "utilization"),
        ("✅ 正确拒答 VALID（负题）", "neg_valid_refusal"),
        ("⚠️ 幻觉 INVALID（负题,越低越好）", "neg_invalid_halluc"),
        ("答题率", "answer_rate"),
    ]
    for label, k in rows:
        L.append(f"| {label} | " + " | ".join(str(s["per_system"][x].get(k)) for x in sysl) + " |")

    L.append("\n## 2. 成对盲评胜率（位置对冲）\n")
    for p in s["pairwise"].values():
        L.append(
            f"- **{p['s1']} vs {p['s2']}**（{p['n']} 题）："
            f"{p['s1']} 胜 {p['w1']} ｜ {p['s2']} 胜 {p['w2']} ｜ 平 {p['tie']}"
        )

    L.append("\n## 3. 头条：RAG vs 裸LLM，按 spec 冷门度拆\n")
    L.append("> 假设：RAG 在长尾冷门 spec 上优势最大（裸 LLM 预训练记不住冷门细节）。\n")
    for label, zh in (("core", "核心 series(23/38/29/24/33)"), ("tail", "长尾 series")):
        h = s["headline"][label]
        n = next(iter(h.values()))["n"] if h else 0
        L.append(f"\n**{zh}**（{n} 题）")
        L.append("| 系统 | fact_coverage | spec 归属命中 |")
        L.append("|---|---|---|")
        for x in sysl:
            L.append(f"| {x} | {h[x]['fact_coverage']} | {h[x]['spec_hit']} |")

    L.append("\n## 4. 检索 vs 生成拆解（A/B）\n")
    L.append("| 系统 | 检索到(recall) | 答出来(coverage) | 利用率 |")
    L.append("|---|---|---|---|")
    for x in sysl:
        ps = s["per_system"][x]
        if ps.get("fact_in_context_recall") is not None:
            L.append(
                f"| {x} | {ps['fact_in_context_recall']} | "
                f"{ps['fact_coverage']} | {ps['utilization']} |"
            )
    L.append(
        "\n- recall 高 coverage 低 → 生成/prompt 没把检索到的料用出来；recall 低 → 检索问题；"
        "\n- C 无 recall（无检索）但 coverage 不为零 = 靠预训练记忆（RAG 未起作用）。\n"
    )
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    scores = json.loads(args.scores.read_text(encoding="utf-8"))
    md = render_markdown(scores)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md, encoding="utf-8")
    print(f"report → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
