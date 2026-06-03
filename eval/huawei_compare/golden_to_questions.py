"""golden_compare.yaml → collect 用的 `{item_id, question}` JSONL。

采集层（collect_a/b/c）只读 item_id + question；本脚本把 golden 题集摊平成那个最小格式。

用法（eval venv，PYTHONPATH=/data/3GPP-Everything）：
    eval/.venv/bin/python -m eval.huawei_compare.golden_to_questions \
        --golden eval/huawei_compare/golden_compare.yaml \
        --out eval-results/huawei-compare/questions.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.runner_retrieval import load_golden


def golden_to_questions(golden_path: Path) -> list[dict]:
    return [{"item_id": it.id, "question": it.question} for it in load_golden(golden_path)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    rows = golden_to_questions(args.golden)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} questions → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
