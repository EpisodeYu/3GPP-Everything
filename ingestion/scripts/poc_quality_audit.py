"""POC 静态质量抽检：在 38331_chunks.jsonl 上分析 table / formula / figure。

输出 markdown 表格 + JSON 抽样到 stdout / 文件，供 Claude 人审。

抽检维度：
1. table chunks（markdown 表格语法）：
   - 是否含有效的 markdown table header + separator
   - 行数 / 列数 / 字符数分布
   - inline LaTeX 是否在 cells 内（KaTeX inline `$..$` 渲染）
2. formula chunks（LaTeX 块）：
   - 是否被 `$$..$$` 包围
   - 字符数分布
   - 是否含 KaTeX 不支持的环境（如 `\begin{eqnarray}`、`\textcolor`）
3. figure chunks（vision 描述）：
   - 抽 10 张展示 description / labels / acronyms / figure_kind
   - 报告 vision 命中率
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path


def load_chunks(path: str) -> list[dict]:
    chunks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks


TABLE_HEADER_RE = re.compile(r"^\s*\|.+\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")


def audit_tables(chunks: list[dict]) -> dict:
    tables = [c for c in chunks if c["chunk_type"] == "table"]
    stats = {
        "count": len(tables),
        "char_min": None,
        "char_max": None,
        "char_median": None,
        "with_separator": 0,
        "with_inline_latex": 0,
        "row_distribution": Counter(),
        "examples": [],
    }
    if not tables:
        return stats

    char_lens = []
    for t in tables:
        c = t["content"]
        char_lens.append(len(c))
        lines = c.splitlines()
        non_empty = [ln for ln in lines if ln.strip()]
        has_sep = any(TABLE_SEP_RE.match(ln) for ln in non_empty)
        if has_sep:
            stats["with_separator"] += 1
        if "$" in c:
            stats["with_inline_latex"] += 1
        pipe_rows = sum(1 for ln in non_empty if "|" in ln)
        stats["row_distribution"][min(pipe_rows, 50)] += 1

    char_lens.sort()
    stats["char_min"] = char_lens[0]
    stats["char_max"] = char_lens[-1]
    stats["char_median"] = char_lens[len(char_lens) // 2]
    short = [t for t in tables if 200 < len(t["content"]) < 1500][:3]
    mid = [t for t in tables if 1500 <= len(t["content"]) < 4000][:3]
    long = [t for t in tables if len(t["content"]) >= 4000][:2]
    has_latex = [t for t in tables if "$" in t["content"]][:2]
    for t in short + mid + long + has_latex:
        stats["examples"].append(
            {
                "chunk_id": t["chunk_id"][:8],
                "clause": t["clause"],
                "section_title": t["section_title"][:60],
                "char_count": len(t["content"]),
                "content": t["content"],
            }
        )
    return stats


KATEX_UNSUPPORTED = [
    r"\eqnarray",
    r"\eqalign",
    r"\textcolor",
    r"\href",
    r"\nobreakspace",
    r"\bbox",
]


def audit_formulas(chunks: list[dict]) -> dict:
    formulas = [c for c in chunks if c["chunk_type"] == "formula"]
    stats = {
        "count": len(formulas),
        "char_min": None,
        "char_max": None,
        "char_median": None,
        "with_dollar_dollar": 0,
        "with_inline_dollar_only": 0,
        "with_unsupported_katex": 0,
        "unsupported_examples": [],
        "examples": [],
    }
    if not formulas:
        return stats
    char_lens = []
    for f in formulas:
        c = f["content"]
        char_lens.append(len(c))
        if "$$" in c:
            stats["with_dollar_dollar"] += 1
        elif "$" in c:
            stats["with_inline_dollar_only"] += 1
        for sym in KATEX_UNSUPPORTED:
            if sym in c:
                stats["with_unsupported_katex"] += 1
                stats["unsupported_examples"].append(
                    {
                        "chunk_id": f["chunk_id"][:8],
                        "clause": f["clause"],
                        "unsupported": sym,
                        "content": c[:500],
                    }
                )
                break
    char_lens.sort()
    stats["char_min"] = char_lens[0]
    stats["char_max"] = char_lens[-1]
    stats["char_median"] = char_lens[len(char_lens) // 2]
    short = [f for f in formulas if len(f["content"]) < 400][:2]
    mid = [f for f in formulas if 400 <= len(f["content"]) < 1500][:3]
    long = [f for f in formulas if len(f["content"]) >= 1500][:3]
    for f in short + mid + long:
        stats["examples"].append(
            {
                "chunk_id": f["chunk_id"][:8],
                "clause": f["clause"],
                "section_title": f["section_title"][:80],
                "char_count": len(f["content"]),
                "content": f["content"],
            }
        )
    return stats


def audit_figures(chunks: list[dict]) -> dict:
    figures = [c for c in chunks if c["chunk_type"] == "figure"]
    stats = {
        "count": len(figures),
        "with_vision": 0,
        "by_figure_kind": Counter(),
        "vision_undescribable": 0,
        "examples": [],
        "label_count_median": 0,
        "acronym_count_median": 0,
        "description_chars_median": 0,
    }
    if not figures:
        return stats
    desc_lens = []
    label_counts = []
    acro_counts = []
    for f in figures:
        v = (f.get("raw_extra") or {}).get("vision")
        if v:
            stats["with_vision"] += 1
            kind = v.get("figure_kind", "?")
            stats["by_figure_kind"][kind] += 1
            if kind == "undescribable":
                stats["vision_undescribable"] += 1
            desc = v.get("description", "")
            desc_lens.append(len(desc))
            label_counts.append(len(v.get("visible_labels") or []))
            acro_counts.append(len(v.get("visible_acronyms") or []))
    if desc_lens:
        desc_lens.sort()
        label_counts.sort()
        acro_counts.sort()
        stats["description_chars_median"] = desc_lens[len(desc_lens) // 2]
        stats["label_count_median"] = label_counts[len(label_counts) // 2]
        stats["acronym_count_median"] = acro_counts[len(acro_counts) // 2]

    sample = figures[:: max(1, len(figures) // 10)][:10]
    for f in sample:
        v = (f.get("raw_extra") or {}).get("vision") or {}
        re_ = f.get("raw_extra") or {}
        stats["examples"].append(
            {
                "chunk_id": f["chunk_id"][:8],
                "clause": f["clause"],
                "section_title": f["section_title"][:80],
                "image_path": re_.get("image_path", "")[-80:],
                "figure_kind": v.get("figure_kind"),
                "description": v.get("description", ""),
                "visible_labels": v.get("visible_labels", []),
                "visible_acronyms": v.get("visible_acronyms", []),
                "spec_role": v.get("spec_role", ""),
                "gsma_caption": re_.get("gsma_caption_text", "")[:200],
                "spec_caption": re_.get("spec_caption", ""),
                "content_head": f["content"][:300],
            }
        )
    return stats


def render_markdown(stats: dict, out_path: str, source: str, total_chunks: int) -> None:
    lines = [
        "# 38.331 POC chunks 静态质量抽检\n",
        f"_source: `{source}` · total chunks: {total_chunks}_\n",
        "## 1. Table chunks\n",
        f"- count = **{stats['tables']['count']}**",
        f"- char range = {stats['tables']['char_min']} ~ {stats['tables']['char_max']}, "
        f"median = {stats['tables']['char_median']}",
        f"- with markdown `|-|-|` separator = "
        f"{stats['tables']['with_separator']}/{stats['tables']['count']} "
        f"({stats['tables']['with_separator'] * 100 // max(1, stats['tables']['count'])}%)",
        f"- with inline `$..$` LaTeX = {stats['tables']['with_inline_latex']}",
        "",
        "### 1.1 Table samples",
    ]
    for i, ex in enumerate(stats["tables"]["examples"], 1):
        lines.append(
            f"\n#### Sample T{i} · clause={ex['clause']} · {ex['char_count']} chars\n"
            f"_section: {ex['section_title']}_  ·  _chunk_id: {ex['chunk_id']}_\n"
        )
        lines.append("```markdown")
        body = ex["content"]
        if len(body) > 2500:
            body = body[:2500] + "\n... (truncated)"
        lines.append(body)
        lines.append("```")

    lines.append("\n## 2. Formula chunks\n")
    lines.append(f"- count = **{stats['formulas']['count']}**")
    lines.append(
        f"- char range = {stats['formulas']['char_min']} ~ "
        f"{stats['formulas']['char_max']}, median = {stats['formulas']['char_median']}"
    )
    lines.append(f"- with `$$..$$` block = {stats['formulas']['with_dollar_dollar']}")
    lines.append(
        f"- with `$..$` inline only (no `$$`) = " f"{stats['formulas']['with_inline_dollar_only']}"
    )
    lines.append(
        f"- with KaTeX-unsupported pattern = {stats['formulas']['with_unsupported_katex']}"
    )
    if stats["formulas"]["unsupported_examples"]:
        lines.append("\n### 2.1 Unsupported (need preprocessing before KaTeX)\n")
        for ex in stats["formulas"]["unsupported_examples"][:3]:
            lines.append(
                f"- clause={ex['clause']} · `{ex['unsupported']}` · "
                f"chunk_id={ex['chunk_id']}\n```\n{ex['content']}\n```"
            )
    lines.append("\n### 2.2 Formula samples\n")
    for i, ex in enumerate(stats["formulas"]["examples"], 1):
        lines.append(
            f"\n#### Sample F{i} · clause={ex['clause']} · {ex['char_count']} chars\n"
            f"_section: {ex['section_title']}_  ·  _chunk_id: {ex['chunk_id']}_\n"
        )
        lines.append("```latex")
        body = ex["content"]
        if len(body) > 1500:
            body = body[:1500] + "\n... (truncated)"
        lines.append(body)
        lines.append("```")

    lines.append("\n## 3. Figure chunks (vision)\n")
    lines.append(f"- count = **{stats['figures']['count']}**")
    lines.append(
        f"- with vision JSON = {stats['figures']['with_vision']}/{stats['figures']['count']}"
    )
    lines.append(f"- figure_kind distribution = {dict(stats['figures']['by_figure_kind'])}")
    lines.append(f"- undescribable = {stats['figures']['vision_undescribable']}")
    lines.append(f"- description chars median = {stats['figures']['description_chars_median']}")
    lines.append(f"- visible_labels count median = {stats['figures']['label_count_median']}")
    lines.append(f"- visible_acronyms count median = {stats['figures']['acronym_count_median']}")
    lines.append("\n### 3.1 Vision sample (every ~6th chunk)\n")
    for i, ex in enumerate(stats["figures"]["examples"], 1):
        lines.append(
            f"\n#### Sample V{i} · clause={ex['clause']} · figure_kind={ex['figure_kind']}\n"
            f"_section: {ex['section_title']}_\n"
            f"_image: ...{ex['image_path']}_\n"
            f"\n**Description**:\n\n> {ex['description']}\n"
        )
        if ex["visible_labels"]:
            lines.append(
                f"**Visible labels** ({len(ex['visible_labels'])}): "
                + ", ".join(f"`{x}`" for x in ex["visible_labels"][:20])
            )
        if ex["visible_acronyms"]:
            lines.append(
                f"\n**Visible acronyms** ({len(ex['visible_acronyms'])}): "
                + ", ".join(f"`{x}`" for x in ex["visible_acronyms"][:20])
            )
        if ex["spec_role"]:
            lines.append(f"\n**Spec role**: {ex['spec_role']}")
        if ex["spec_caption"]:
            lines.append(f"\n**Original spec caption**: {ex['spec_caption']}")

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote audit → {out_path}")


def main(jsonl_path: str, out_md: str) -> int:
    chunks = load_chunks(jsonl_path)
    print(f"loaded {len(chunks)} chunks")
    stats = {
        "tables": audit_tables(chunks),
        "formulas": audit_formulas(chunks),
        "figures": audit_figures(chunks),
    }
    print("table count:", stats["tables"]["count"])
    print("formula count:", stats["formulas"]["count"])
    print("figure count:", stats["figures"]["count"])
    render_markdown(stats, out_md, jsonl_path, len(chunks))
    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1] if len(sys.argv) > 1 else "/data/tgpp/poc/38331_chunks.jsonl",
            sys.argv[2] if len(sys.argv) > 2 else "/data/tgpp/poc/38331_quality_audit.md",
        )
    )
