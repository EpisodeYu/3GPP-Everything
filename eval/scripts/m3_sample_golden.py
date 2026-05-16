"""从 T2 v1.draft.yaml 智能抽样 → v1.yaml（Gate-2 路径 C）。

抽样策略：
1. category 分层：小类（negative/multi_section/formula）全收；大类按目标数量限量
2. category 内部按 spec_id 分桶，round-robin 拿题，确保 spec 多样性
3. random.seed=42 保证可复现

默认目标 120 题；实际数量取决于 draft 总量与各 category 实际数。
"""

from __future__ import annotations

import argparse
import logging
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_SEED = 42

# category 目标数量；小类（≤ 5 当全收）自动全收，大类按下表限量
DEFAULT_CATEGORY_TARGETS: dict[str, int] = {
    "definition": 60,
    "procedure": 40,
    "table_lookup": 12,
    # 下面三类如果 draft 内题数 ≤ 此数 → 全收（实际 negative/multi_section=3, formula=1）
    "multi_section": 10,
    "negative": 10,
    "formula": 10,
    "tool": 10,
}


def stratified_sample(
    draft_items: list[dict],
    *,
    category_targets: dict[str, int] = DEFAULT_CATEGORY_TARGETS,
    seed: int = DEFAULT_SEED,
) -> list[dict]:
    """category 分层 + spec round-robin 抽样。

    输入：draft v1.yaml 的 items list
    输出：抽样后的 items（题目原样保留，仅子集）
    """
    rng = random.Random(seed)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for it in draft_items:
        by_cat[str(it.get("category", "unknown"))].append(it)

    sampled: list[dict] = []
    log.info("draft category distribution: %s", {k: len(v) for k, v in by_cat.items()})

    for cat, items in by_cat.items():
        target = category_targets.get(cat, 10)
        if len(items) <= target:
            sampled.extend(items)
            log.info("%-14s | full pick %d items (target=%d)", cat, len(items), target)
            continue

        # 按 spec_id 分桶；多 spec 的题归到第一个 whitelist spec_id 上
        by_spec: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            specs = it.get("expected_specs") or []
            first = (specs[0].get("spec_id") if specs and isinstance(specs[0], dict) else "") or ""
            by_spec[first].append(it)
        # 每桶内 shuffle 保证抽样多样
        for bucket in by_spec.values():
            rng.shuffle(bucket)

        # round-robin 从各 spec 桶里拿，直到达到 target
        spec_order = sorted(by_spec.keys())  # 字典序稳定
        rng.shuffle(spec_order)  # 顺序也用 rng 打乱（同 seed 可复现）
        picked: list[dict] = []
        idx = 0
        while len(picked) < target:
            progress = False
            for spec in spec_order:
                if len(picked) >= target:
                    break
                bucket = by_spec[spec]
                if idx < len(bucket):
                    picked.append(bucket[idx])
                    progress = True
            idx += 1
            if not progress:
                break  # 所有桶都用完了仍未到 target
        sampled.extend(picked)
        log.info(
            "%-14s | sampled %d / %d (target=%d, %d specs)",
            cat,
            len(picked),
            len(items),
            target,
            len(by_spec),
        )

    return sampled


def write_v1_yaml(
    sampled_items: list[dict],
    *,
    out_path: Path,
    version: int = 1,
) -> None:
    """按 06-...md §3.5 schema 写 v1.yaml。重新分配 id（按 category 排序）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sampled_items = sorted(
        sampled_items, key=lambda x: (x["category"], x.get("teleqna_origin_id", ""))
    )
    cats_counter: dict[str, int] = defaultdict(int)
    short = {
        "definition": "def",
        "procedure": "proc",
        "multi_section": "multi",
        "table_lookup": "table",
        "formula": "form",
        "tool": "tool",
        "negative": "neg",
    }
    for it in sampled_items:
        cats_counter[it["category"]] += 1
        prefix = short.get(it["category"], "qa")
        it["id"] = f"{prefix}-{cats_counter[it['category']]:03d}"

    doc = {
        "version": version,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "total": len(sampled_items),
        "sources": ["teleqna_transformed"],
        "categories": sorted({i["category"] for i in sampled_items}),
        "items": sampled_items,
    }
    out_path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120),
        encoding="utf-8",
    )
    log.info("wrote v1.yaml: %s (n=%d)", out_path, len(sampled_items))


def report_distribution(sampled: list[dict]) -> dict:
    """打印 + 返回采样后的 category / spec 分布。"""
    by_cat: dict[str, int] = defaultdict(int)
    by_spec: dict[str, int] = defaultdict(int)
    for it in sampled:
        by_cat[it["category"]] += 1
        for s in it.get("expected_specs") or []:
            sid = (s or {}).get("spec_id")
            if sid:
                by_spec[sid] += 1
    return {
        "total": len(sampled),
        "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
        "by_spec": dict(sorted(by_spec.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--draft",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "golden" / "v1.draft.yaml",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "golden" / "v1.yaml",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--definition", type=int, default=DEFAULT_CATEGORY_TARGETS["definition"])
    parser.add_argument("--procedure", type=int, default=DEFAULT_CATEGORY_TARGETS["procedure"])
    parser.add_argument(
        "--table_lookup", type=int, default=DEFAULT_CATEGORY_TARGETS["table_lookup"]
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")
    doc = yaml.safe_load(args.draft.read_text(encoding="utf-8"))
    items = doc.get("items") or []
    log.info("loaded draft: %d items from %s", len(items), args.draft)

    targets = dict(DEFAULT_CATEGORY_TARGETS)
    targets["definition"] = args.definition
    targets["procedure"] = args.procedure
    targets["table_lookup"] = args.table_lookup

    sampled = stratified_sample(items, category_targets=targets, seed=args.seed)
    write_v1_yaml(sampled, out_path=args.out)

    report = report_distribution(sampled)
    import json as _json

    print(_json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
