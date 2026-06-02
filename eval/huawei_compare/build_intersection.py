"""枚举 A∩B 的 R18 交集 spec 清单——100 题对比题集的采样范围。

- A = 3GPP-Everything 已索引 spec：`INGEST_DATA_DIR/bm25/voyage/by_spec/*.jsonl` 文件名。
- B = 华为 Telco-RAG 离线库 `netop/Embeddings3GPP-R18` 的 `Documents/*.docx` 文件名
  （从 HuggingFace 树 API 取，不必下载 3.3GB）。

公平性：题集只能落在 A∩B，否则一方无库可检 → 不可比。交集为 R18 口径（B 仅 R18）。

用法：
    uv run --project eval python -m eval.huawei_compare.build_intersection
    # → 写 eval/huawei_compare/r18_intersection_specs.txt + 打印 series 分布
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

HF_TREE_URL = (
    "https://huggingface.co/api/datasets/netop/Embeddings3GPP-R18/tree/main?recursive=true"
)
OUT_PATH = Path(__file__).parent / "r18_intersection_specs.txt"

# spec 主体：NN.NNN，可带多部件后缀 -MM（如 TR 23.700-81）；去版本/release 后缀（-i00 / v17）
_SPEC_RE = re.compile(r"(\d{2})[.\-]?(\d{3})(?:-(\d{1,3})\b)?")


def normalize_spec_id(raw: str) -> str | None:
    """`38331-i00.docx` / `23.501.jsonl` / `23700-81-i00` → `38.331` / `23.501` / `23.700-81`。"""
    if not raw:
        return None
    stem = Path(raw).name
    # search 而非 match：兼容 "TS 24.501 v17" 这种带前缀的写法；release 后缀（-i00）
    # 因 group3 要求 '-数字' 而 'i00' 是字母，自然不被吞，保留 -NN 多部件（23.700-81）。
    m = _SPEC_RE.search(stem)
    if not m:
        return None
    base = f"{m.group(1)}.{m.group(2)}"
    return f"{base}-{m.group(3)}" if m.group(3) else base


def extract_a_specs(by_spec_dir: Path) -> set[str]:
    """A 已索引 spec：by_spec 目录下每篇一个 jsonl，文件名即 spec_id。"""
    out: set[str] = set()
    for p in by_spec_dir.iterdir():
        sid = normalize_spec_id(p.name)
        if sid:
            out.add(sid)
    return out


def extract_b_specs(tree: list[dict]) -> set[str]:
    """B 库 spec：HF 树里 `Documents/*.docx` 文件名。"""
    out: set[str] = set()
    for e in tree:
        path = e.get("path", "")
        if (
            e.get("type") == "file"
            and path.startswith("Documents/")
            and path.lower().endswith(".docx")
        ):
            sid = normalize_spec_id(path)
            if sid:
                out.add(sid)
    return out


def fetch_b_tree(url: str = HF_TREE_URL) -> list[dict]:
    with urllib.request.urlopen(url, timeout=60) as resp:  # 固定 HF 域名
        return json.loads(resp.read().decode("utf-8"))


def default_a_dir() -> Path:
    base = os.environ.get("INGEST_DATA_DIR", "/data/tgpp")
    return Path(base) / "bm25" / "voyage" / "by_spec"


def build(a_dir: Path | None = None) -> tuple[list[str], Counter]:
    a_dir = a_dir or default_a_dir()
    a = extract_a_specs(a_dir)
    b = extract_b_specs(fetch_b_tree())
    inter = sorted(a & b)
    series = Counter(s[:2] for s in inter)
    return inter, series


def main() -> int:
    a_dir = default_a_dir()
    if not a_dir.is_dir():
        print(f"[err] A by_spec 目录不存在：{a_dir}（设 INGEST_DATA_DIR）", file=sys.stderr)
        return 2
    inter, series = build(a_dir)
    OUT_PATH.write_text("\n".join(inter) + "\n", encoding="utf-8")
    print(f"交集 = {len(inter)} 篇 → {OUT_PATH}")
    print("series 分布:", dict(sorted(series.items(), key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
