"""chunk_id 漂移检测（M2 → M6 准入硬指标）。

来源：`docs/04-handoff/2026-05-16-m1-to-m2-decisions.md §3` "M3 → M6 过渡硬指标"
+ `eval-results/m2-poc/17specs_throughput.md §5` 草案。

目的：
- M2 已在 17 篇 POC 上索引出 30,558 chunks；chunk_id = uuid5(spec_id|clause|sha256(content)[:16])
  内容稳定 → ID 稳定。
- M3 评测期间会调 chunker 参数（target_tokens / overlap / 注入头 / atomic_blocks
  / vision prompt 等）。任何"chunk content 字面变化"都会导致 chunk_id 集合发生漂移。
- 决议要求：漂移率 ≤ 5% 才能在 M6 全量索引时复用 `--skip-indexed`，
  否则必须 purge POC 17 篇重新 embed（成本 ~$0.6 / 5M tokens）。

漂移率定义（对称差 / 并集，IoU 反向 = 1 - Jaccard）：
    drift = |old_ids △ new_ids| / |old_ids ∪ new_ids|

输入：
- spec_ids（默认 17 篇 POC：M1 38.331 + M2 Task D 16 篇）
- 旧 chunk_id 来源：Qdrant 主 dim collection（默认 `tgpp_chunks_voyage_d1024`；M3 决胜后 2048 已 drop），
  按 spec_id 过滤 scroll
- 新 chunk_id 来源：当前代码 + 当前 chunker 参数，调 `build_chunks`
  （vision_resolver=None，仅取内容字面，与 §3 决议口径一致 ——
  vision 描述在 chunker 之外；如果 vision 改了，已通过 chunk_id 哈希反映）

输出：
- stdout：每 spec 漂移率 + 总体漂移率 + PASS / FAIL（5% 阈值）
- 可选 JSON 报告 (`--out`)
- exit code：0 = 漂移 ≤ 5%；1 = 超阈值（CI / handoff 卡阈值用）

用法：
```
PYTHONPATH=.. uv run python -m ingestion.scripts.m3_chunk_id_drift \\
    --threshold 0.05 \\
    --out /tmp/chunk_id_drift.json
```

可选：
- `--spec-ids 38.331,38.300`  指定子集
- `--threshold 0.05`           漂移阈值（默认 5%）
- `--target-tokens 250`        chunker 参数（默认 plan §0 锁定值）
- `--collection-prefix tgpp_chunks`
- `--main-dim 1024`            从哪个 dim collection 读旧 ids（默认主 dim；M3 决胜后 2048 已 drop）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ingestion.chunker import ChunkParams, build_chunks
from ingestion.hf_loader import (
    GsmaHfLoader,
    dedupe_keep_latest,
    get_meta,
    manifest_session,
    read_entries,
)

log = logging.getLogger("m3_chunk_id_drift")

DEFAULT_POC_17 = [
    "23.401",
    "23.501",
    "23.502",
    "23.503",
    "24.501",
    "29.500",
    "29.501",
    "29.502",
    "29.503",
    "29.518",
    "36.213",
    "38.214",
    "38.300",
    "38.331",
    "38.401",
    "38.413",
    "38.473",
]
DEFAULT_THRESHOLD = 0.05
DEFAULT_QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
DEFAULT_COLLECTION_PREFIX = os.environ.get("QDRANT_COLLECTION_PREFIX", "tgpp_chunks")
DEFAULT_PROVIDER = "voyage"
DEFAULT_MAIN_DIM = 1024  # M3 决胜后单值 1024（2048 collection 已 drop）


@dataclass(slots=True)
class SpecDrift:
    spec_id: str
    old_count: int = 0
    new_count: int = 0
    intersection: int = 0
    only_old: int = 0  # 旧有新无（chunker 改后丢失）
    only_new: int = 0  # 新有旧无（chunker 改后产生）
    drift: float = 0.0
    error: str | None = None


@dataclass(slots=True)
class DriftReport:
    threshold: float
    spec_ids: list[str] = field(default_factory=list)
    total_old: int = 0
    total_new: int = 0
    total_intersection: int = 0
    total_only_old: int = 0
    total_only_new: int = 0
    overall_drift: float = 0.0
    per_spec: list[SpecDrift] = field(default_factory=list)
    chunk_params: dict[str, int] = field(default_factory=dict)
    qdrant_url: str = ""
    collection: str = ""
    elapsed_s: float = 0.0
    passed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "passed": self.passed,
            "overall_drift": self.overall_drift,
            "totals": {
                "old": self.total_old,
                "new": self.total_new,
                "intersection": self.total_intersection,
                "only_old": self.total_only_old,
                "only_new": self.total_only_new,
            },
            "qdrant": {
                "url": self.qdrant_url,
                "collection": self.collection,
            },
            "chunk_params": self.chunk_params,
            "spec_ids": self.spec_ids,
            "per_spec": [
                {
                    "spec_id": s.spec_id,
                    "old": s.old_count,
                    "new": s.new_count,
                    "intersection": s.intersection,
                    "only_old": s.only_old,
                    "only_new": s.only_new,
                    "drift": s.drift,
                    "error": s.error,
                }
                for s in self.per_spec
            ],
            "elapsed_s": self.elapsed_s,
        }


def fetch_old_chunk_ids(
    client: httpx.Client, *, qdrant_url: str, collection: str, spec_id: str
) -> set[str]:
    """从 Qdrant 主 dim collection 按 spec_id scroll 出全部 chunk_id。

    用 payload.chunk_id（写入时落盘的 uuid5 字符串），不依赖 point.id 的 qdrant 内部表示。
    """
    ids: set[str] = set()
    offset: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": 1000,
            "with_payload": ["chunk_id"],
            "with_vector": False,
            "filter": {"must": [{"key": "spec_id", "match": {"value": spec_id}}]},
        }
        if offset is not None:
            body["offset"] = offset
        r = client.post(f"{qdrant_url}/collections/{collection}/points/scroll", json=body)
        r.raise_for_status()
        res = r.json()["result"]
        for p in res["points"]:
            payload = p.get("payload") or {}
            cid = payload.get("chunk_id") or str(p.get("id"))
            ids.add(cid)
        offset = res.get("next_page_offset")
        if not offset:
            break
    return ids


def compute_new_chunk_ids(
    *,
    spec_id: str,
    loader: GsmaHfLoader,
    entries_by_spec: dict[str, Any],
    chunk_params: ChunkParams,
) -> set[str]:
    """用当前代码 + 当前 chunker 参数，对单 spec 跑一次 chunker，收集 chunk_id。

    `vision_resolver=None`：figure chunk 走 GSMA 自带描述 fallback；
    与 M2 索引时一致（M2 vision 已写入 chunk content；如果 vision prompt 后续改动
    导致 figure chunk content 变化，本脚本能捕获到 chunk_id 漂移）。
    """
    entry = entries_by_spec[spec_id]
    bundle = next(loader.iter_specs([entry]))
    chunks, _ = build_chunks(bundle, params=chunk_params, vision_resolver=None)
    return {c.chunk_id for c in chunks}


def diff_chunk_ids(old: set[str], new: set[str]) -> tuple[int, int, int, float]:
    """返回 (intersection, only_old, only_new, drift)。

    drift = |sym_diff| / |union|；空集时返回 0.0（避免 0/0）。
    """
    inter = len(old & new)
    only_old = len(old - new)
    only_new = len(new - old)
    union = len(old | new)
    if union == 0:
        return 0, 0, 0, 0.0
    drift = (only_old + only_new) / union
    return inter, only_old, only_new, drift


def run(
    *,
    spec_ids: list[str],
    threshold: float,
    qdrant_url: str,
    collection: str,
    chunk_params: ChunkParams,
    manifest_path: Path,
    out_path: Path | None = None,
) -> DriftReport:
    t0 = time.time()
    report = DriftReport(
        threshold=threshold,
        spec_ids=list(spec_ids),
        chunk_params={
            "target_tokens": chunk_params.target_tokens,
            "max_tokens": chunk_params.max_tokens,
            "overlap_tokens": chunk_params.overlap_tokens,
            "short_section_threshold": chunk_params.short_section_threshold,
        },
        qdrant_url=qdrant_url,
        collection=collection,
    )

    with manifest_session(manifest_path) as conn:
        all_entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")
    entries_by_spec: dict[str, Any] = {}
    for e in dedupe_keep_latest(list(all_entries)):
        if e.spec_id in set(spec_ids):
            entries_by_spec[e.spec_id] = e
    missing = [s for s in spec_ids if s not in entries_by_spec]
    if missing:
        log.warning("manifest missing spec_ids: %s", missing)

    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)

    with httpx.Client(timeout=60.0) as client:
        for spec_id in spec_ids:
            sd = SpecDrift(spec_id=spec_id)
            try:
                old_ids = fetch_old_chunk_ids(
                    client, qdrant_url=qdrant_url, collection=collection, spec_id=spec_id
                )
                if spec_id not in entries_by_spec:
                    sd.error = "spec not in manifest (skipped chunker)"
                    sd.old_count = len(old_ids)
                    report.per_spec.append(sd)
                    continue
                new_ids = compute_new_chunk_ids(
                    spec_id=spec_id,
                    loader=loader,
                    entries_by_spec=entries_by_spec,
                    chunk_params=chunk_params,
                )
                inter, only_old, only_new, drift = diff_chunk_ids(old_ids, new_ids)
                sd.old_count = len(old_ids)
                sd.new_count = len(new_ids)
                sd.intersection = inter
                sd.only_old = only_old
                sd.only_new = only_new
                sd.drift = round(drift, 6)

                report.total_old += sd.old_count
                report.total_new += sd.new_count
                report.total_intersection += inter
                report.total_only_old += only_old
                report.total_only_new += only_new
                marker = "OK " if drift <= threshold else "FAIL"
                log.info(
                    "[%s] %s old=%d new=%d ∩=%d old\\new=%d new\\old=%d drift=%.2f%%",
                    marker,
                    spec_id,
                    sd.old_count,
                    sd.new_count,
                    inter,
                    only_old,
                    only_new,
                    drift * 100,
                )
            except Exception as exc:
                sd.error = f"{type(exc).__name__}: {exc}"
                log.exception("spec %s drift compute failed", spec_id)
            report.per_spec.append(sd)

    union_total = report.total_intersection + report.total_only_old + report.total_only_new
    if union_total > 0:
        report.overall_drift = round(
            (report.total_only_old + report.total_only_new) / union_total, 6
        )
    report.passed = report.overall_drift <= threshold and not any(s.error for s in report.per_spec)
    report.elapsed_s = round(time.time() - t0, 2)

    log.info(
        "[overall] specs=%d old=%d new=%d ∩=%d drift=%.2f%% threshold=%.2f%% → %s",
        len(report.per_spec),
        report.total_old,
        report.total_new,
        report.total_intersection,
        report.overall_drift * 100,
        threshold * 100,
        "PASS" if report.passed else "FAIL",
    )
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
        log.info("wrote drift report → %s", out_path)
    return report


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="M3 chunk_id 漂移自动检测")
    p.add_argument(
        "--spec-ids",
        type=str,
        default=",".join(DEFAULT_POC_17),
        help="逗号分隔 spec_id 列表（默认 17 篇 POC）",
    )
    p.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="漂移阈值（默认 0.05 = 5%%）"
    )
    p.add_argument("--qdrant-url", type=str, default=DEFAULT_QDRANT_URL)
    p.add_argument("--collection-prefix", type=str, default=DEFAULT_COLLECTION_PREFIX)
    p.add_argument("--provider", type=str, default=DEFAULT_PROVIDER)
    p.add_argument(
        "--main-dim",
        type=int,
        default=DEFAULT_MAIN_DIM,
        help="主 dim collection 后缀（默认 1024，从 `_d1024` 读旧 ids；M3 决胜后 2048 已 drop）",
    )
    p.add_argument("--target-tokens", type=int, default=ChunkParams().target_tokens)
    p.add_argument("--max-tokens", type=int, default=ChunkParams().max_tokens)
    p.add_argument("--overlap-tokens", type=int, default=ChunkParams().overlap_tokens)
    p.add_argument(
        "--short-section-threshold",
        type=int,
        default=ChunkParams().short_section_threshold,
    )
    p.add_argument(
        "--manifest",
        type=Path,
        default=Path(os.environ.get("INGEST_DATA_DIR") or "/data/tgpp")
        / "markdown"
        / "gsma_manifest.sqlite",
    )
    p.add_argument("--out", type=Path, default=None, help="可选 JSON 报告输出路径")
    p.add_argument("--log-level", type=str, default=os.environ.get("LOG_LEVEL", "INFO"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    spec_ids = [s.strip() for s in args.spec_ids.split(",") if s.strip()]
    if not spec_ids:
        log.error("--spec-ids parsed to empty list")
        return 2
    collection = f"{args.collection_prefix}_{args.provider}_d{args.main_dim}"
    chunk_params = ChunkParams(
        target_tokens=args.target_tokens,
        max_tokens=args.max_tokens,
        overlap_tokens=args.overlap_tokens,
        short_section_threshold=args.short_section_threshold,
    )
    report = run(
        spec_ids=spec_ids,
        threshold=args.threshold,
        qdrant_url=args.qdrant_url,
        collection=collection,
        chunk_params=chunk_params,
        manifest_path=args.manifest,
        out_path=args.out,
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
