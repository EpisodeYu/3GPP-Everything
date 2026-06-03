"""合并 A、B（、C）采集结果 → 统一 `results.json`（eval venv）。

A（collect_a）/ C（collect_c）已是 SystemAnswer-dict；B（collect_b）是精简 raw，
这里用 `schema.b_raw_to_answer` 解析成 SystemAnswer（切 contexts + 抽 cited_specs），
再按 item_id 对齐。C（裸 LLM 基线）可选。

用法（eval venv）：
    uv run --project eval python -m eval.huawei_compare.merge_results \
        --a .../a_answers.jsonl --b .../b_raw.jsonl [--c .../c_answers.jsonl] \
        --out .../results.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from eval.huawei_compare.schema import align_systems, b_raw_to_answer, load_jsonl


def build_results(a_path: Path, b_path: Path, c_path: Path | None = None) -> dict:
    by_sys: dict[str, list[dict]] = {
        "A": load_jsonl(a_path),
        "B": [b_raw_to_answer(r).to_dict() for r in load_jsonl(b_path)],
    }
    if c_path is not None:
        by_sys["C"] = load_jsonl(c_path)
    result = align_systems(by_sys)
    result["generated_at"] = datetime.now(UTC).isoformat()
    result["systems"] = list(by_sys.keys())
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", dest="a_path", required=True, type=Path)
    ap.add_argument("--b", dest="b_path", required=True, type=Path)
    ap.add_argument("--c", dest="c_path", type=Path, default=None)
    ap.add_argument("--out", dest="out_path", required=True, type=Path)
    args = ap.parse_args()

    result = build_results(args.a_path, args.b_path, args.c_path)
    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    args.out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    sysset = result["systems"]
    allp = sum(1 for it in result["items"] if all(it.get(s) for s in sysset))
    print(f"合并完成：{result['n_items']} 题 → {args.out_path}（{'/'.join(sysset)} 都有={allp}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
