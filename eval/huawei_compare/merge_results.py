"""合并 A、B 两系统采集结果 → 统一 `results.json`（eval venv）。

A 侧（collect_a.py）已是 SystemAnswer-dict；B 侧（collect_b.py）是精简 raw，
这里用 `schema.b_raw_to_answer` 解析成 SystemAnswer（切 contexts + 抽 cited_specs），
再按 item_id 对齐。

用法（eval venv）：
    uv run --project eval python -m eval.huawei_compare.merge_results \
        --a eval-results/huawei-compare-smoke/a_answers.jsonl \
        --b eval-results/huawei-compare-smoke/b_raw.jsonl \
        --out eval-results/huawei-compare-smoke/results.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from eval.huawei_compare.schema import align, b_raw_to_answer, load_jsonl


def build_results(a_path: Path, b_path: Path) -> dict:
    a_records = load_jsonl(a_path)
    b_records = [b_raw_to_answer(r).to_dict() for r in load_jsonl(b_path)]
    result = align(a_records, b_records)
    result["generated_at"] = datetime.now(UTC).isoformat()
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", dest="a_path", required=True, type=Path)
    ap.add_argument("--b", dest="b_path", required=True, type=Path)
    ap.add_argument("--out", dest="out_path", required=True, type=Path)
    args = ap.parse_args()

    result = build_results(args.a_path, args.b_path)
    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    args.out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    both = sum(1 for it in result["items"] if it["A"] and it["B"])
    print(f"合并完成：{result['n_items']} 题 → {args.out_path}（A&B 都有={both}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
