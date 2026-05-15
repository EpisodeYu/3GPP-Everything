"""方案 E 上线前 mini benchmark（docs §4.2.5 第 1 项）。

目的：在 12 张跨 8 类真实 GSMA figure 上跑 PROMPT_E_UNIFIED，
验证 vision.py 单次调用的：

1. JSON 解析成功率 ≥ 95%（12 张 ≥ 12 张通过；少 1 张允许）
2. description 长度与 v2 B（mimo free-text）相当（不显著缩水）
3. visible_labels / visible_acronyms 数量合理

输出：
- eval-results/source-audit/vision_e_validate.md
- 与 v2 benchmark 报告对比 description 长度

成本预算：12 次 mimo 调用 + Redis 缓存命中后零成本；首次约 15-20k completion tokens
（v2 C 单次 ~2456 ct，方案 E 因 prompt 略短预计 1500-2000 ct/次 × 12 ≈ 18-24k tokens）。
低于 CLAUDE.md §5.2 阈值（100 次 / 1M token），无需事先报批。

环境：
- LITELLM_BASE_URL / LITELLM_API_KEY 必须在 .env 中
- INGEST_DATA_DIR/markdown/gsma_manifest.sqlite 已存在（先跑 hf-pull）
- 默认走 Redis（REDIS_URL）；禁用缓存可设 VISION_E_VALIDATE_NO_CACHE=1

用法：
    cd ingestion
    uv run python scripts/vision_e_validate.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, "/home/s1yu/3GPP-Everything")

from ingestion.chunker.atomic_blocks import parse_atomic_blocks
from ingestion.chunker.figure import extract_figure
from ingestion.chunker.tokenize_utils import count_tokens
from ingestion.hf_loader import GsmaHfLoader, dedupe_keep_latest, resolve_image
from ingestion.hf_loader.manifest_store import get_meta, manifest_session, read_entries
from ingestion.images.vision import (
    DEFAULT_MAX_TOKENS,
    VisionResolver,
    _LiteLLMClient,
    _VisionCache,
    make_default_image_loader,
)
from ingestion.scripts.vision_strategy_benchmark import SAMPLES

REPORT_PATH = Path(
    "/home/s1yu/3GPP-Everything/eval-results/source-audit/vision_e_validate.md"
)


@dataclass(slots=True)
class FigureSample:
    kind_label: str
    spec_id: str
    clause: str | None
    image_basename: str
    image_path: str
    section_title: str
    image_size: int
    image_sha256: str


@dataclass(slots=True)
class CallStats:
    sample: FigureSample
    cached: bool
    ok: bool
    description: str = ""
    description_tokens: int = 0
    figure_kind: str = ""
    visible_labels_count: int = 0
    visible_acronyms_count: int = 0
    spec_role: str = ""
    completion_tokens: int = 0
    reasoning_tokens: int | None = None
    elapsed_s: float = 0.0
    error: str = ""
    structured: dict = field(default_factory=dict)


def _load_samples() -> list[FigureSample]:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    manifest_path = Path(base) / "markdown" / "gsma_manifest.sqlite"
    if not manifest_path.exists():
        raise SystemExit(f"manifest not found: {manifest_path}; 先跑 ingestion hf-pull")
    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")

    by_spec: dict[str, list] = {}
    for kind, spec_id, clause, basename in SAMPLES:
        by_spec.setdefault(spec_id, []).append((kind, clause, basename))

    loader = GsmaHfLoader(revision=revision, token=os.environ.get("HF_TOKEN") or None)
    samples: list[FigureSample] = []
    for spec_id, items in by_spec.items():
        cands = [e for e in entries if e.spec_id == spec_id]
        if not cands:
            print(f"  WARN: spec {spec_id} not in manifest")
            continue
        entry = dedupe_keep_latest(cands)[0]
        spec_dir = entry.raw_md_path.rsplit("/", 1)[0]
        for bundle in loader.iter_specs([entry]):
            for sec in bundle.sections:
                blocks = parse_atomic_blocks(sec.body)
                for blk in blocks:
                    if blk.kind != "figure":
                        continue
                    ext = extract_figure(blk)
                    if ext is None:
                        continue
                    bn = ext.image_path.rsplit("/", 1)[-1]
                    for kind, clause, target_bn in list(items):
                        if bn != target_bn:
                            continue
                        if clause is not None and sec.clause != clause:
                            continue
                        repo_image_path = (
                            ext.image_path
                            if ext.image_path.startswith("marked/")
                            else f"{spec_dir}/{bn}"
                        )
                        try:
                            img = resolve_image(
                                repo_image_path,
                                revision=revision,
                                token=os.environ.get("HF_TOKEN"),
                            )
                        except Exception as exc:
                            print(f"  ERR: resolve {repo_image_path} failed: {exc}")
                            continue
                        samples.append(
                            FigureSample(
                                kind_label=kind,
                                spec_id=spec_id,
                                clause=sec.clause,
                                image_basename=bn,
                                image_path=repo_image_path,
                                section_title=sec.section_title,
                                image_size=img.size,
                                image_sha256=img.sha256,
                            )
                        )
                        items.remove((kind, clause, target_bn))
                        if not items:
                            break
                if not items:
                    break
            break
        if items:
            print(f"  WARN: not found in {spec_id}: {items}")

    order = {(spec_id, basename): i for i, (_, spec_id, _, basename) in enumerate(SAMPLES)}
    samples.sort(key=lambda s: order.get((s.spec_id, s.image_basename), 999))
    return samples


def _build_resolver() -> VisionResolver:
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    if not base_url or not api_key:
        raise SystemExit("LITELLM_BASE_URL / LITELLM_API_KEY 必须在 .env 中提供")
    http = _LiteLLMClient(base_url=base_url, api_key=api_key)

    no_cache = os.environ.get("VISION_E_VALIDATE_NO_CACHE") == "1"
    if no_cache:
        cache = _VisionCache(redis_client=None)  # 强制禁用
        # 强制 cache._client = None
        cache._client = None  # type: ignore[attr-defined]
    else:
        cache = _VisionCache()

    revision = None
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    manifest_path = Path(base) / "markdown" / "gsma_manifest.sqlite"
    if manifest_path.exists():
        with manifest_session(manifest_path) as conn:
            revision = get_meta(conn, "last_pull_revision")

    return VisionResolver(
        http_client=http,
        cache=cache,
        image_loader=make_default_image_loader(
            revision=revision, token=os.environ.get("HF_TOKEN") or None
        ),
        model=os.environ.get("LLM_VISION_MODEL") or "mimo-v2.5",
        max_tokens=DEFAULT_MAX_TOKENS,
        max_retries=2,  # benchmark 阶段失败快放回；生产用 3
    )


def _run_one(resolver: VisionResolver, sample: FigureSample) -> CallStats:
    ctx = {
        "spec_id": sample.spec_id,
        "clause": sample.clause or "",
        "section_title": sample.section_title,
        "image_alt": "",
        "spec_caption": "",
    }
    t0 = time.time()
    try:
        out = resolver(sample.image_path, ctx)
    except Exception as exc:
        return CallStats(
            sample=sample,
            cached=False,
            ok=False,
            elapsed_s=time.time() - t0,
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = time.time() - t0
    if out is None:
        return CallStats(
            sample=sample,
            cached=False,
            ok=False,
            elapsed_s=elapsed,
            error="resolver returned None (dead-letter or image load fail)",
        )
    desc = out.get("description", "")
    return CallStats(
        sample=sample,
        cached=bool(out.get("cached")),
        ok=True,
        description=desc,
        description_tokens=count_tokens(desc),
        figure_kind=str(out.get("figure_kind", "")),
        visible_labels_count=len(out.get("visible_labels") or []),
        visible_acronyms_count=len(out.get("visible_acronyms") or []),
        spec_role=str(out.get("spec_role", "")),
        completion_tokens=int(out.get("completion_tokens") or 0),
        reasoning_tokens=out.get("reasoning_tokens"),
        elapsed_s=elapsed,
        structured=out,
    )


def _render_report(stats_list: list[CallStats], *, model: str, no_cache: bool) -> str:
    total = len(stats_list)
    ok_calls = [s for s in stats_list if s.ok]
    cached_calls = [s for s in ok_calls if s.cached]
    fresh_calls = [s for s in ok_calls if not s.cached]
    fail_calls = [s for s in stats_list if not s.ok]

    parse_ok_rate = (len(ok_calls) / total) if total else 0.0
    threshold_pass = parse_ok_rate >= 0.95

    fresh_tokens = [s.description_tokens for s in fresh_calls]
    all_tokens = [s.description_tokens for s in ok_calls]
    completion_tokens = [s.completion_tokens for s in fresh_calls if s.completion_tokens > 0]
    elapsed = [s.elapsed_s for s in fresh_calls if s.elapsed_s > 0]
    labels_counts = [s.visible_labels_count for s in ok_calls]
    acronyms_counts = [s.visible_acronyms_count for s in ok_calls]

    def _stats(values: list[int | float]) -> str:
        if not values:
            return "n/a"
        median = statistics.median(values)
        return (
            f"min={min(values):.0f} median={median:.0f} max={max(values):.0f} "
            f"mean={sum(values) / len(values):.1f}"
        )

    lines: list[str] = []
    lines.append("# Vision 方案 E 上线前 mini benchmark（PROMPT_E_UNIFIED 验证）")
    lines.append("")
    lines.append("- benchmark date: 2026-05-15")
    lines.append(f"- vision model: `{model}` (via LiteLLM proxy)")
    lines.append(
        f"- samples: {total} figures across {len({s.sample.kind_label for s in stats_list})} kinds"
    )
    lines.append("- prompt: `PROMPT_E_UNIFIED`（与 docs §4.2.1 一字一句对齐）")
    lines.append(f"- max_tokens: {DEFAULT_MAX_TOKENS}, max_retries: 2 (benchmark 用，生产 3)")
    lines.append(f"- Redis 缓存：{'禁用 (VISION_E_VALIDATE_NO_CACHE=1)' if no_cache else '启用'}")
    lines.append("")

    lines.append("## 1. 验收指标")
    lines.append("")
    lines.append("| 指标 | 阈值 | 实测 | 结论 |")
    lines.append("|------|------|------|------|")
    lines.append(
        f"| JSON 解析成功率（含 normalize 通过） | ≥ 95% | "
        f"**{len(ok_calls)}/{total} = {parse_ok_rate * 100:.1f}%** | "
        f"{'✅' if threshold_pass else '❌'} |"
    )
    if all_tokens:
        median_tokens = int(statistics.median(all_tokens))
        # v2 benchmark 报告里 B median = 331 tokens；E 应该不显著低于这个
        b_baseline = 331
        keeps_quality = median_tokens >= b_baseline * 0.6  # 容许 -40% 余地
        lines.append(
            f"| description median tokens vs v2 B (331) | ≥ 60% baseline | "
            f"**{median_tokens}** | {'✅' if keeps_quality else '⚠️'} |"
        )
    else:
        lines.append("| description median tokens | ≥ 60% baseline | n/a | ❌ |")
    lines.append("")

    lines.append("## 2. 调用统计")
    lines.append("")
    lines.append(f"- 总样本: {total}")
    lines.append(f"- 成功: {len(ok_calls)}（其中缓存命中: {len(cached_calls)}, 新调用: {len(fresh_calls)}）")
    lines.append(f"- 失败: {len(fail_calls)}")
    lines.append("")
    lines.append("description tokens 分布（成功样本）:")
    lines.append(f"- 全部 ok 样本：{_stats([float(x) for x in all_tokens])}")
    lines.append(f"- 仅新调用（无缓存）：{_stats([float(x) for x in fresh_tokens])}")
    lines.append("")
    lines.append("API 用量（仅新调用）:")
    lines.append(f"- completion_tokens: {_stats([float(x) for x in completion_tokens])}")
    lines.append(f"- elapsed_s: {_stats(elapsed)}")
    lines.append(
        f"- 总 completion_tokens: {sum(completion_tokens)}（按 mimo-v2.5 vision 价格估算成本）"
    )
    lines.append("")
    lines.append("结构化字段:")
    lines.append(f"- visible_labels 数：{_stats([float(x) for x in labels_counts])}")
    lines.append(f"- visible_acronyms 数：{_stats([float(x) for x in acronyms_counts])}")
    lines.append("")

    lines.append("## 3. 单图详细结果")
    lines.append("")
    for s in stats_list:
        sample = s.sample
        lines.append(
            f"### [{sample.kind_label}] {sample.spec_id} clause {sample.clause or '-'} — `{sample.image_basename}`"
        )
        lines.append("")
        lines.append(f"- section_title: {sample.section_title}")
        lines.append(
            f"- image: {sample.image_size} bytes, sha256 `{sample.image_sha256[:16]}...`"
        )
        if not s.ok:
            lines.append(f"- 状态：❌ {s.error}")
            lines.append("")
            continue
        cache_tag = "📦 cached" if s.cached else "🆕 fresh"
        lines.append(
            f"- 状态：✅ {cache_tag} · figure_kind=`{s.figure_kind}` · spec_role=`{s.spec_role}`"
        )
        lines.append(
            f"- description: {s.description_tokens} tokens / {len(s.description)} chars"
        )
        lines.append(
            f"- visible_labels: {s.visible_labels_count} · visible_acronyms: {s.visible_acronyms_count}"
        )
        if not s.cached:
            lines.append(
                f"- API: ct={s.completion_tokens} rt={s.reasoning_tokens} elapsed={s.elapsed_s:.1f}s"
            )
        lines.append("")
        lines.append("**description**:")
        lines.append("")
        lines.append("```")
        truncated = s.description[:1500]
        lines.append(truncated + ("..." if len(s.description) > 1500 else ""))
        lines.append("```")
        if s.structured:
            vl = s.structured.get("visible_labels") or []
            va = s.structured.get("visible_acronyms") or []
            lines.append(f"- visible_labels (first 20): {vl[:20]}")
            lines.append(f"- visible_acronyms (first 20): {va[:20]}")
            if s.structured.get("undescribable_reason"):
                lines.append(f"- undescribable_reason: {s.structured['undescribable_reason']}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## 4. 结论")
    lines.append("")
    if threshold_pass:
        lines.append("✅ **PROMPT_E_UNIFIED 通过 §4.2.5 第 1 项验收门槛**：可上线 vision.py 主路径。")
    else:
        lines.append(
            "❌ **未通过验收门槛**：JSON 解析成功率 < 95%。"
            "在 vision.py 上线前需修 prompt / 加更激进的 JSON 提取兜底。"
        )
    lines.append("")
    if all_tokens and statistics.median(all_tokens) < 331 * 0.6:
        lines.append(
            "⚠️ description median tokens 显著低于 v2 B baseline（331）。"
            "可能是 mimo 把 token budget 分给了结构化字段；"
            "需要做下游检索回归验证（M3 评测期）确认是否影响召回准确率。"
        )
        lines.append("")
    lines.append("- 缓存命中率与 description 长度细节见 §2 / §3。")
    lines.append("- 若发现某类 figure 系统性失败，单独记进 `eval-results/source-audit/`。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_本报告由 `ingestion/scripts/vision_e_validate.py` 生成；用于 docs §4.2.5 第 1 项门禁。_"
    )
    return "\n".join(lines)


def main() -> int:
    print(f"[vision-e-validate] loading {len(SAMPLES)} samples ...")
    samples = _load_samples()
    print(f"[vision-e-validate] loaded {len(samples)} samples")

    no_cache = os.environ.get("VISION_E_VALIDATE_NO_CACHE") == "1"
    resolver = _build_resolver()
    model = resolver._model  # type: ignore[attr-defined]

    stats_list: list[CallStats] = []
    try:
        for i, sample in enumerate(samples, 1):
            print(
                f"[{i}/{len(samples)}] {sample.kind_label} | {sample.spec_id} | "
                f"{sample.image_basename}"
            )
            stats = _run_one(resolver, sample)
            if stats.ok:
                tag = "cached" if stats.cached else "fresh"
                print(
                    f"     ✅ {tag} kind={stats.figure_kind} "
                    f"desc_tokens={stats.description_tokens} "
                    f"labels={stats.visible_labels_count} "
                    f"acronyms={stats.visible_acronyms_count} "
                    f"ct={stats.completion_tokens} "
                    f"elapsed={stats.elapsed_s:.1f}s"
                )
            else:
                print(f"     ❌ {stats.error}")
            stats_list.append(stats)
    finally:
        resolver.close()

    md = _render_report(stats_list, model=model, no_cache=no_cache)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(md, encoding="utf-8")
    ok_count = sum(1 for s in stats_list if s.ok)
    print(
        f"\n[vision-e-validate] {ok_count}/{len(stats_list)} ok "
        f"({ok_count / len(stats_list) * 100 if stats_list else 0:.1f}%)"
    )
    print(f"[vision-e-validate] report → {REPORT_PATH}")
    if ok_count / len(stats_list) < 0.95 if stats_list else True:
        print("[vision-e-validate] ❌ below 95% threshold")
        return 1

    # 把成功样本的 description 长度 dump 到 stdout 末尾，便于跟 v2 B 对比
    desc_tokens = [s.description_tokens for s in stats_list if s.ok]
    if desc_tokens:
        print(
            f"[vision-e-validate] description tokens median="
            f"{int(statistics.median(desc_tokens))} "
            f"(v2 B baseline: 331; v2 C: 169)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
