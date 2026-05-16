"""TeleQnA 过滤：raw.jsonl → filtered.jsonl + out_of_scope.jsonl。

硬约束（2026-05-16 决议）：
- 题目必须严格属于 17 篇 POC spec whitelist
- expected_specs 推断不到 whitelist 内任一 spec_id → 进 out_of_scope.jsonl

过滤管线（轻量、零 LLM 成本）：
1. category 命中：'Standards specifications' / 'Standards overview'
2. 文本扫描：question + explanation 中抽取所有 spec_id（NN.NNN 格式 + alias）
3. 与 whitelist 求交集：
   - 命中 ≥ 1 → 进 filtered.jsonl，标 `expected_specs_inferred`
   - 命中 = 0 → 进 out_of_scope.jsonl，标拒因
4. 关键词加权（可选）：5G 相关术语命中 → `topic_score`（用于 T2 分层抽样）

不做：
- LLM 二次确认（留给 T2 转化阶段一并判，省 token）
- "选项排除题" 检测（同上）
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .whitelist import (
    POC_17_SPECS,
    extract_all_spec_ids,
    load_spec_aliases,
    normalize_spec_id,
)

log = logging.getLogger(__name__)

DEFAULT_CATEGORIES_KEEP = frozenset({"Standards specifications", "Standards overview"})

# 5G 相关高信号关键词（不强制命中，只用于 topic_score 排序 / 分层抽样）
TOPIC_KEYWORDS_5G = (
    "5G",
    "5GS",
    "5G System",
    "5G Core",
    "NR",
    "New Radio",
    "AMF",
    "SMF",
    "UPF",
    "AUSF",
    "UDM",
    "UDR",
    "PCF",
    "NRF",
    "NEF",
    "NWDAF",
    "NSSF",
    "SBA",
    "SBI",
    "Service Based Architecture",
    "PDU Session",
    "QoS Flow",
    "RRC",
    "PDCP",
    "RLC",
    "MAC",
    "gNB",
    "ng-eNB",
    "gNB-CU",
    "gNB-DU",
    "Xn",
    "F1",
    "E1",
    "NG-AP",
    "XnAP",
    "F1AP",
    "E1AP",
    "Network Slice",
    "S-NSSAI",
    "Registration",
    "Authentication",
    "Mobility",
)


@dataclass(slots=True)
class FilterStats:
    total: int = 0
    kept: int = 0
    rejected_category: int = 0
    rejected_no_spec: int = 0
    out_of_scope: int = 0  # spec 推断成功但全部 ∉ whitelist
    by_spec: dict[str, int] = None  # type: ignore[assignment]
    by_category: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_spec is None:
            self.by_spec = {}
        if self.by_category is None:
            self.by_category = {}

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "kept": self.kept,
            "rejected_category": self.rejected_category,
            "rejected_no_spec": self.rejected_no_spec,
            "out_of_scope": self.out_of_scope,
            "by_spec": dict(sorted(self.by_spec.items())),
            "by_category": dict(self.by_category),
        }


def _infer_specs_from_item(
    item: dict,
    *,
    aliases: dict[str, str] | None = None,
    explicit_field: str = "explanation",
) -> tuple[list[str], list[str]]:
    """抽取一条 TeleQnA item 中所有引用到的 spec_id。

    扫源：question + explanation + 各 option 字段。
    返回 (all_specs_seen, specs_in_whitelist)。
    """
    aliases = aliases or {}
    text_chunks = [str(item.get("question", ""))]
    explanation = str(item.get(explicit_field, "") or "")
    if explanation:
        text_chunks.append(explanation)
    for k, v in item.items():
        if isinstance(k, str) and k.startswith("option ") and isinstance(v, str):
            text_chunks.append(v)
    big_text = "\n".join(text_chunks)
    raw_specs = extract_all_spec_ids(big_text)

    canonical: list[str] = []
    seen: set[str] = set()
    for s in raw_specs:
        c = aliases.get(s, s)
        # 再过一次 normalize（防 alias 写得不规范）
        c = normalize_spec_id(c) or c
        if c and c not in seen:
            seen.add(c)
            canonical.append(c)

    in_white = [s for s in canonical if s in POC_17_SPECS]
    return canonical, in_white


@lru_cache(maxsize=8)
def _kw_patterns(keywords: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """编译 word-boundary 正则，避免 "NR" 子串命中 "u**nr**elated"。"""
    return tuple(re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in keywords)


def _topic_score(text: str, keywords: Iterable[str] = TOPIC_KEYWORDS_5G) -> int:
    """关键词命中数（按 keyword 列表去重）。题目分布报告时用于排序。

    使用 word-boundary 匹配，避免如 "NR" 命中 "unrelated" / "AMF" 命中 "famfest"
    这类子串误判。
    """
    if not text:
        return 0
    patterns = _kw_patterns(tuple(keywords))
    return sum(1 for p in patterns if p.search(text))


def _category_keep(item: dict, categories_keep: frozenset[str]) -> bool:
    cat = str(item.get("category", "") or "")
    return cat in categories_keep


def filter_one(
    item: dict,
    *,
    aliases: dict[str, str] | None = None,
    categories_keep: frozenset[str] = DEFAULT_CATEGORIES_KEEP,
) -> tuple[str, dict]:
    """对单条 raw item 决策。

    返回 (verdict, enriched_item)，verdict ∈ {"kept", "rejected_category",
    "rejected_no_spec", "out_of_scope"}。
    """
    if not _category_keep(item, categories_keep):
        return "rejected_category", item

    all_specs, in_white = _infer_specs_from_item(item, aliases=aliases)
    enriched = dict(item)
    enriched["specs_seen"] = all_specs
    enriched["expected_specs_inferred"] = in_white
    enriched["topic_score_5g"] = _topic_score(
        str(item.get("question", "")) + "\n" + str(item.get("explanation", "") or "")
    )

    if not all_specs:
        return "rejected_no_spec", enriched
    if not in_white:
        enriched["reject_reason"] = f"specs_seen={all_specs} all out of POC 17-whitelist"
        return "out_of_scope", enriched

    return "kept", enriched


def filter_jsonl(
    *,
    raw_jsonl: Path,
    out_dir: Path,
    categories_keep: frozenset[str] = DEFAULT_CATEGORIES_KEEP,
    aliases: dict[str, str] | None = None,
) -> FilterStats:
    """raw.jsonl → out_dir/{filtered,out_of_scope}.jsonl + filter_stats.json。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    aliases = aliases if aliases is not None else load_spec_aliases()
    stats = FilterStats()

    kept_path = out_dir / "filtered.jsonl"
    oos_path = out_dir / "out_of_scope.jsonl"
    stats_path = out_dir / "filter_stats.json"

    with (
        raw_jsonl.open("r", encoding="utf-8") as f_in,
        kept_path.open("w", encoding="utf-8") as f_kept,
        oos_path.open("w", encoding="utf-8") as f_oos,
    ):
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            stats.total += 1
            cat = str(item.get("category", "") or "(no-cat)")
            stats.by_category[cat] = stats.by_category.get(cat, 0) + 1

            verdict, enriched = filter_one(item, aliases=aliases, categories_keep=categories_keep)
            if verdict == "kept":
                stats.kept += 1
                for s in enriched.get("expected_specs_inferred") or []:
                    stats.by_spec[s] = stats.by_spec.get(s, 0) + 1
                f_kept.write(json.dumps(enriched, ensure_ascii=False) + "\n")
            elif verdict == "rejected_category":
                stats.rejected_category += 1
            elif verdict == "rejected_no_spec":
                stats.rejected_no_spec += 1
            elif verdict == "out_of_scope":
                stats.out_of_scope += 1
                f_oos.write(json.dumps(enriched, ensure_ascii=False) + "\n")

    stats_path.write_text(
        json.dumps(stats.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "filter done: total=%d kept=%d rejected_category=%d rejected_no_spec=%d "
        "out_of_scope=%d → %s",
        stats.total,
        stats.kept,
        stats.rejected_category,
        stats.rejected_no_spec,
        stats.out_of_scope,
        kept_path,
    )
    return stats


__all__ = [
    "DEFAULT_CATEGORIES_KEEP",
    "TOPIC_KEYWORDS_5G",
    "FilterStats",
    "filter_jsonl",
    "filter_one",
]
