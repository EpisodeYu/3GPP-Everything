"""把已落地的 results.json 在新 golden 上重算 substring 类 metric。

用途：修了 expected_facts / forbidden 之后，**不重跑 agent**，直接在原 answer
上重算 fact_coverage / forbidden_violations，省 LLM 钱。retrieval / ragas /
negative_judge 这些不依赖 golden answer-string 的指标按原值复用。

仅适用于"golden 改了 expected_facts / forbidden 但 question / spec_id 没改"
的场景。如果 golden question / expected_specs 也动了，需要重跑 agent。

用法：

    uv run --project eval python -m eval.scripts.recompute_substring_metrics \\
        --results eval-results/m7-post-m75-baseline/results.json \\
        --old-golden eval/golden/v1.yaml \\
        --new-golden eval/golden/v1.repaired.yaml \\
        --out eval-results/m7.7-post-golden-repair/report.md
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

import yaml


def _safe_mean(xs: list[float | None]) -> float | None:
    vs = [v for v in xs if v is not None]
    return mean(vs) if vs else None


def _norm_strs(items: list[Any]) -> list[str]:
    """YAML 可能把 '1024' 这种 fact 解析成 int；统一 stringify。"""
    out: list[str] = []
    for f in items or []:
        s = str(f).strip() if f is not None else ""
        if s:
            out.append(s)
    return out


def _fact_coverage(answer: str, expected_facts: list[Any]) -> float | None:
    facts = _norm_strs(expected_facts)
    if not facts:
        return None
    hay = (answer or "").lower()
    hits = sum(1 for f in facts if f.lower() in hay)
    return hits / len(facts)


def _forbidden_hits(answer: str, forbidden: list[Any]) -> list[str]:
    hay = (answer or "").lower()
    return [f for f in _norm_strs(forbidden) if f.lower() in hay]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--old-golden", required=True, type=Path)
    ap.add_argument("--new-golden", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    with args.results.open("r", encoding="utf-8") as f:
        data = json.load(f)
    with args.old_golden.open("r", encoding="utf-8") as f:
        old_g = {it["id"]: it for it in yaml.safe_load(f)["items"]}
    with args.new_golden.open("r", encoding="utf-8") as f:
        new_g = {it["id"]: it for it in yaml.safe_load(f)["items"]}

    rows = data["results"]
    # group by source / category
    delta_rows: list[dict[str, Any]] = []
    new_by_source_cat: dict[str, dict[str, list[float | None]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in rows:
        gid = r["item_id"]
        old = old_g.get(gid, {})
        new = new_g.get(gid, {})
        if not new:
            continue
        source = new.get("source", "?")
        cat = new.get("category", "?")
        old_fc = r.get("fact_coverage")
        old_fb = r.get("forbidden_violations") or []
        new_fc = _fact_coverage(r.get("answer", ""), new.get("expected_facts") or [])
        new_fb = _forbidden_hits(r.get("answer", ""), new.get("forbidden") or [])
        delta_rows.append(
            {
                "item_id": gid,
                "source": source,
                "category": cat,
                "fact_old": old_fc,
                "fact_new": new_fc,
                "fact_delta": (new_fc - old_fc)
                if (old_fc is not None and new_fc is not None)
                else None,
                "fb_old": len(old_fb),
                "fb_new": len(new_fb),
                "fb_added_or_removed": sorted(
                    set([f for f in (old.get("forbidden") or [])])
                    ^ set([f for f in (new.get("forbidden") or [])])
                ),
            }
        )
        new_by_source_cat[source]["fact_coverage_new"].append(new_fc)
        new_by_source_cat[source]["fact_coverage_old"].append(old_fc)
        new_by_source_cat[source]["forbidden_hit_new"].append(1.0 if new_fb else 0.0)
        new_by_source_cat[source]["forbidden_hit_old"].append(1.0 if old_fb else 0.0)
        # also by category within source
        scat = f"{source}::{cat}"
        new_by_source_cat[scat]["fact_coverage_new"].append(new_fc)
        new_by_source_cat[scat]["fact_coverage_old"].append(old_fc)
        new_by_source_cat[scat]["forbidden_hit_new"].append(1.0 if new_fb else 0.0)
        new_by_source_cat[scat]["forbidden_hit_old"].append(1.0 if old_fb else 0.0)

    # ALL aggregate
    all_fc_new = [d["fact_new"] for d in delta_rows if d["fact_new"] is not None]
    all_fc_old = [d["fact_old"] for d in delta_rows if d["fact_old"] is not None]
    all_fb_new = sum(1 for d in delta_rows if d["fb_new"] > 0) / len(delta_rows) if delta_rows else 0
    all_fb_old = sum(1 for d in delta_rows if d["fb_old"] > 0) / len(delta_rows) if delta_rows else 0

    lines: list[str] = []
    lines.append("# Substring 指标重算（不重跑 agent）")
    lines.append("")
    lines.append(f"- 原 results: `{args.results}`")
    lines.append(f"- 旧 golden : `{args.old_golden}`")
    lines.append(f"- 新 golden : `{args.new_golden}`")
    lines.append(f"- n_items   : {len(delta_rows)}")
    lines.append("")
    lines.append("## 整体 (overall)")
    lines.append("")
    lines.append(
        f"- fact_coverage  : old=**{mean(all_fc_old):.3f}** → new=**{mean(all_fc_new):.3f}** (Δ {mean(all_fc_new)-mean(all_fc_old):+.3f})"
    )
    lines.append(
        f"- forbidden hit rate (any): old=**{all_fb_old:.1%}** → new=**{all_fb_new:.1%}** (Δ {(all_fb_new-all_fb_old)*100:+.1f}pp)"
    )
    lines.append("")
    lines.append("## by source")
    lines.append("")
    lines.append("| source | fact_coverage old | fact_coverage new | Δ | forbidden hit% old | forbidden hit% new |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for src in sorted(s for s in new_by_source_cat if "::" not in s):
        v = new_by_source_cat[src]
        fc_o, fc_n = _safe_mean(v["fact_coverage_old"]), _safe_mean(v["fact_coverage_new"])
        fb_o, fb_n = (
            (sum(v["forbidden_hit_old"]) / len(v["forbidden_hit_old"])) if v["forbidden_hit_old"] else 0,
            (sum(v["forbidden_hit_new"]) / len(v["forbidden_hit_new"])) if v["forbidden_hit_new"] else 0,
        )
        delta = (fc_n - fc_o) if fc_o is not None and fc_n is not None else None
        lines.append(
            f"| {src} | {fc_o:.3f} | {fc_n:.3f} | {delta:+.3f} | {fb_o:.1%} | {fb_n:.1%} |"
            if delta is not None
            else f"| {src} | — | — | — | — | — |"
        )
    lines.append("")
    lines.append("## by source × category")
    lines.append("")
    lines.append("| key | fact_coverage old | fact_coverage new | Δ | forbidden hit% old | forbidden hit% new |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for key in sorted(s for s in new_by_source_cat if "::" in s):
        v = new_by_source_cat[key]
        fc_o, fc_n = _safe_mean(v["fact_coverage_old"]), _safe_mean(v["fact_coverage_new"])
        fb_o, fb_n = (
            (sum(v["forbidden_hit_old"]) / len(v["forbidden_hit_old"])) if v["forbidden_hit_old"] else 0,
            (sum(v["forbidden_hit_new"]) / len(v["forbidden_hit_new"])) if v["forbidden_hit_new"] else 0,
        )
        if fc_o is None or fc_n is None:
            lines.append(f"| {key} | — | — | — | {fb_o:.1%} | {fb_n:.1%} |")
            continue
        delta = fc_n - fc_o
        lines.append(
            f"| {key} | {fc_o:.3f} | {fc_n:.3f} | {delta:+.3f} | {fb_o:.1%} | {fb_n:.1%} |"
        )
    lines.append("")
    # Top biggest fact_coverage gains
    lines.append("## 最大 fact_coverage 提升 (top-15)")
    lines.append("")
    gainers = [d for d in delta_rows if d["fact_delta"] is not None and d["fact_delta"] > 0]
    gainers.sort(key=lambda d: -d["fact_delta"])
    lines.append("| item_id | source | category | old → new | Δ |")
    lines.append("|---|---|---|---:|---:|")
    for d in gainers[:15]:
        lines.append(
            f"| {d['item_id']} | {d['source']} | {d['category']} | {d['fact_old']:.2f} → {d['fact_new']:.2f} | {d['fact_delta']:+.2f} |"
        )
    lines.append("")
    # Regressions
    losers = [d for d in delta_rows if d["fact_delta"] is not None and d["fact_delta"] < 0]
    losers.sort(key=lambda d: d["fact_delta"])
    lines.append(f"## 回退（如果有，应该手审 LLM rewrite 是否过简） ({len(losers)} 条)")
    lines.append("")
    if losers:
        lines.append("| item_id | source | category | old → new | Δ |")
        lines.append("|---|---|---|---:|---:|")
        for d in losers[:15]:
            lines.append(
                f"| {d['item_id']} | {d['source']} | {d['category']} | {d['fact_old']:.2f} → {d['fact_new']:.2f} | {d['fact_delta']:+.2f} |"
            )
    else:
        lines.append("（无）")
    lines.append("")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote: {args.out}")
    print()
    print(
        f"overall fact_coverage  : {mean(all_fc_old):.3f} → {mean(all_fc_new):.3f} ({mean(all_fc_new)-mean(all_fc_old):+.3f})"
    )
    print(
        f"overall forbidden hit% : {all_fb_old:.1%} → {all_fb_new:.1%} ({(all_fb_new-all_fb_old)*100:+.1f}pp)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
