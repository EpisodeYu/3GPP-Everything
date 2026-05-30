"""把 v1.handwritten.yaml 中指定 item_ids 的 expected_facts + notes 同步进 v1.yaml。

避免手动 yaml StrReplace 出错（两文件格式不同：handwritten 用 multi-line block 字符串 +
list-of-strings 缩进，v1.yaml 是单引号 inline list）。

跑法：
    cd /data/3GPP-Everything && uv run --project eval python -m \\
        eval.scripts.sync_handwritten_to_v1 \\
        --item-ids hand-table-006,hand-formula-001,hand-formula-004,hand-formula-006,hand-formula-007,hand-formula-008,hand-multi-002
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--src",
        type=Path,
        default=Path("eval/golden/v1.handwritten.yaml"),
    )
    ap.add_argument(
        "--dst",
        type=Path,
        default=Path("eval/golden/v1.yaml"),
    )
    ap.add_argument("--item-ids", required=True, help="comma separated")
    args = ap.parse_args()

    ids = {s.strip() for s in args.item_ids.split(",") if s.strip()}
    src = yaml.safe_load(args.src.read_text(encoding="utf-8"))
    dst = yaml.safe_load(args.dst.read_text(encoding="utf-8"))

    src_by_id = {it["id"]: it for it in src["items"]}
    n_updated = 0
    for it in dst["items"]:
        if it.get("id") in ids:
            s = src_by_id.get(it["id"])
            if not s:
                print(f"  MISSING in src: {it['id']}")
                continue
            old_facts = list(it.get("expected_facts") or [])
            new_facts = list(s.get("expected_facts") or [])
            it["expected_facts"] = new_facts
            it["notes"] = s.get("notes", it.get("notes", ""))
            print(
                f"  updated {it['id']}: facts {len(old_facts)} → {len(new_facts)} "
                f"(+{len(new_facts) - len(old_facts)})"
            )
            n_updated += 1

    args.dst.write_text(
        yaml.safe_dump(dst, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"wrote {args.dst} ({n_updated} items synced)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
