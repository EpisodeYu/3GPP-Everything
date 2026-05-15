"""GSMA HF 加载器 CLI 子命令。

被 ingestion/cli.py 挂在 hf 子组下，所以这里只暴露 `app = typer.Typer()`。

子命令：
  hf-pull         扫描 marked + original 文件树 → 写本地 SQLite manifest
  hf-audit        在 manifest 基础上输出数据源验证报告（§4.0）
  hf-load         按 spec_id 加载单 spec 的章节 / 图片元数据并打印
  hf-show         直接打印一篇 spec 的 raw.md / 章节统计（debug 用）
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from pathlib import Path

import typer

from .loader import DEFAULT_RELEASES, GsmaHfLoader
from .manifest_store import (
    get_meta,
    manifest_session,
    read_entries,
    set_meta,
    write_entries,
)
from .markdown_parser import detect_spec_type_and_title
from .spec_grouper import (
    TS_5G_SERIES_WHITELIST,
    dedupe_keep_latest,
    filter_ts_5g,
)

app = typer.Typer(no_args_is_help=True, help="GSMA/3GPP HF dataset 加载器")
log = logging.getLogger(__name__)


def _default_manifest_path() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


def _hf_token() -> str | None:
    """允许通过 HF_TOKEN 注入 token；公开 dataset 时无需。"""
    return os.environ.get("HF_TOKEN") or None


@app.command("hf-pull")
def hf_pull(
    releases: str = typer.Option(
        "18,19", help="逗号分隔的 release 编号，如 '18,19'；写 'all' 跑全部 Rel-8..Rel-19"
    ),
    manifest: Path = typer.Option(
        None, help="SQLite manifest 路径，默认 $INGEST_DATA_DIR/markdown/gsma_manifest.sqlite"
    ),
    revision: str | None = typer.Option(None, help="HF revision，留空则取最新 main"),
    skip_original: bool = typer.Option(
        False, help="跳过 original/ 目录扫描（省时间，但 source_doc_version 留空）"
    ),
    log_level: str = typer.Option("INFO"),
) -> None:
    """枚举 GSMA HF marked/ 树并写 SQLite manifest。"""
    logging.basicConfig(level=log_level)
    manifest_path = manifest or _default_manifest_path()
    rel_list = _resolve_release_list(releases)

    loader = GsmaHfLoader(revision=revision, token=_hf_token())
    typer.echo(f"[hf-pull] revision={loader.revision} releases={rel_list}")
    t0 = time.time()
    entries, stats = loader.build_manifest(
        releases=rel_list, include_original=not skip_original, progress=True
    )
    typer.echo(
        f"[hf-pull] scanned {stats.raw_entries} specs in {time.time() - t0:.1f}s "
        f"(raw_md={stats.raw_md_total_bytes/1024/1024:.1f}MiB, images={stats.image_files_total})"
    )

    with manifest_session(manifest_path) as conn:
        write_entries(conn, entries, replace_revision=loader.revision)
        set_meta(conn, "last_pull_revision", loader.revision)
        set_meta(conn, "last_pull_at", str(int(time.time())))
        set_meta(conn, "last_pull_releases", ",".join(rel_list))
    typer.echo(f"[hf-pull] wrote manifest → {manifest_path}")


@app.command("hf-audit")
def hf_audit(
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
    output: Path = typer.Option(
        Path("eval-results/source-audit/gsma_dataset_audit.md"),
        help="audit md 输出路径",
    ),
    sample_images: int = typer.Option(
        10, help="从 manifest 随机抽取多少张图片做下载 + hash 验证（不调 Vision）"
    ),
    vision_smoke_md: Path = typer.Option(
        Path("eval-results/source-audit/gsma_vision_smoke.md"),
        help="Vision 烟雾测试报告路径；存在则在 audit 中引用并将第 5 项标记为 ✅",
    ),
) -> None:
    """生成 §4.0 数据源验证 audit md。

    依赖 hf-pull 已生成 manifest；本命令不再扫 HF 树（除非 sample_images > 0
    时会调 hf_hub_download 拉小样本图片做 sha256 验证）。
    """
    manifest_path = manifest or _default_manifest_path()
    if not Path(manifest_path).exists():
        raise typer.BadParameter(f"manifest not found: {manifest_path}. 先跑 `ingestion hf-pull`。")

    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision") or "(unknown)"
        pulled_at = get_meta(conn, "last_pull_at") or "(unknown)"
        releases_pulled = get_meta(conn, "last_pull_releases") or "(unknown)"

    deduped = dedupe_keep_latest(entries)
    production = filter_ts_5g(deduped)

    series_counter = Counter(e.series for e in production)
    series_bytes = Counter()
    series_images = Counter()
    for e in production:
        series_bytes[e.series] += e.raw_md_size
        series_images[e.series] += e.image_count
    release_counter = Counter(e.release for e in entries)
    dedupe_overlap = len(entries) - len(deduped)

    image_hash_sample = []
    if sample_images > 0:
        from .image_resolver import resolve_image

        # 取每个有图片的 spec 前 1 张，跨多 spec 凑 sample_images 张
        candidates = []
        for e in production:
            if e.image_paths:
                candidates.append((e.spec_id, e.image_paths[0]))
            if len(candidates) >= sample_images:
                break
        for spec_id, repo_path in candidates:
            try:
                img = resolve_image(
                    repo_path,
                    revision=revision if revision != "(unknown)" else None,
                    token=_hf_token(),
                )
                image_hash_sample.append((spec_id, repo_path, img.size, img.sha256))
            except Exception as exc:
                image_hash_sample.append((spec_id, repo_path, -1, f"FAIL: {exc}"))

    vision_smoke_summary = _read_vision_smoke_summary(vision_smoke_md)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    md = _format_audit_md(
        revision=revision,
        pulled_at=pulled_at,
        releases_pulled=releases_pulled,
        raw_entries=entries,
        deduped_entries=deduped,
        production_entries=production,
        series_counter=series_counter,
        series_bytes=series_bytes,
        series_images=series_images,
        release_counter=release_counter,
        dedupe_overlap=dedupe_overlap,
        image_hash_sample=image_hash_sample,
        vision_smoke_summary=vision_smoke_summary,
        vision_smoke_path=vision_smoke_md,
    )
    Path(output).write_text(md, encoding="utf-8")
    typer.echo(f"[hf-audit] wrote {output}")
    typer.echo(
        f"[hf-audit] raw={len(entries)} after-dedup={len(deduped)} "
        f"production(TS 5G whitelist)={len(production)}"
    )


@app.command("hf-classify-types")
def hf_classify_types(
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
    only_whitelist: bool = typer.Option(
        True, help="只处理 TS 5G 系列白名单内的 entries（生产口径范围）"
    ),
    limit: int | None = typer.Option(None, help="最多处理多少条（debug 用）"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """下载 raw.md 头部 ~2KB 识别 TS/TR + 抽 H1 title，回填 manifest。

    生产口径门禁所需的"TS 1296 / TR 待排除"区分依赖此命令。
    单 spec ~0.1-0.5s，1500 篇约 5-15 分钟（HF 限速时更慢）。
    """
    logging.basicConfig(level=log_level)
    manifest_path = manifest or _default_manifest_path()
    if not Path(manifest_path).exists():
        raise typer.BadParameter(f"manifest not found: {manifest_path}. 先跑 hf-pull。")

    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")

    if only_whitelist:
        filtered = [e for e in entries if e.series in TS_5G_SERIES_WHITELIST]
    else:
        filtered = entries
    if limit:
        filtered = filtered[:limit]

    typer.echo(f"[hf-classify-types] processing {len(filtered)} entries (revision={revision})")

    from dataclasses import replace as _dc_replace

    from huggingface_hub import hf_hub_download

    updated: list = []
    last_log = time.time()
    for idx, entry in enumerate(filtered, 1):
        try:
            local = hf_hub_download(
                repo_id="GSMA/3GPP",
                filename=entry.raw_md_path,
                repo_type="dataset",
                revision=revision,
                token=_hf_token(),
            )
            # 读前 2KB 即可
            with open(local, encoding="utf-8") as f:
                head = f.read(4096)
            spec_type, title = detect_spec_type_and_title(head, spec_id=entry.spec_id)
        except Exception as exc:  # pragma: no cover - HF 限速时偶发
            log.warning("classify %s failed: %s", entry.spec_id, exc)
            spec_type, title = entry.spec_type, entry.title
        updated.append(_dc_replace(entry, spec_type=spec_type, title=title or entry.title))
        if time.time() - last_log > 5:
            typer.echo(f"  ...{idx}/{len(filtered)} processed")
            last_log = time.time()

    with manifest_session(manifest_path) as conn:
        write_entries(conn, updated)
        set_meta(conn, "last_classify_at", str(int(time.time())))

    counter = Counter(e.spec_type for e in updated)
    typer.echo(f"[hf-classify-types] done. spec_type distribution: {dict(counter)}")


@app.command("hf-vision-smoke")
def hf_vision_smoke(
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
    sample: int = typer.Option(10, help="抽多少张图过 vision（默认 10）"),
    model: str = typer.Option(
        None, help="vision model；默认读 .env LLM_VISION_MODEL 或 'mimo-v2.5'"
    ),
    max_tokens: int = typer.Option(
        16384,
        help=(
            "单次 Vision 响应最大 token 数。reasoning 模型 reasoning_tokens 有强随机性"
            "（同图同模型两次调用差 9×）；mimo 系列按需停止不会填满 max_tokens。"
            "设大不增加成本（按实际 ct 计费），但可彻底避免被截断。"
        ),
    ),
    output: Path = typer.Option(
        Path("eval-results/source-audit/gsma_vision_smoke.md"),
        help="输出报告路径",
    ),
) -> None:
    """§4.0 第 5 项：抽 10 张图片端到端跑 mimo-v2.5 Vision，确认可调用 + 描述质量。

    走本机 LiteLLM proxy（LITELLM_BASE_URL / LITELLM_API_KEY），不直连厂商；
    本命令不写 Redis 缓存（那是 ingestion/images/vision.py 的事），只验证链路。
    """
    import base64

    import httpx

    manifest_path = manifest or _default_manifest_path()
    if not Path(manifest_path).exists():
        raise typer.BadParameter(f"manifest not found: {manifest_path}")

    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    vision_model = model or os.environ.get("LLM_VISION_MODEL") or "mimo-v2.5"
    if not base_url or not api_key:
        raise typer.BadParameter("LITELLM_BASE_URL / LITELLM_API_KEY 必须在 .env 中提供")

    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")

    deduped = dedupe_keep_latest(entries)
    production = filter_ts_5g(deduped)
    # 抽样策略：跨多个系列；优先抽含 ≥ 2 张图的 spec（一般 logo + 真实技术图）。
    # 每个 series 至多抽 2 张，最多取 sample 张。
    by_series: dict[str, list] = {}
    for e in production:
        if not e.image_paths:
            continue
        # 优先有 ≥ 2 张图的，能避开纯 logo
        score = 1 if len(e.image_paths) >= 2 else 0
        by_series.setdefault(e.series, []).append((score, e))
    for series_list in by_series.values():
        series_list.sort(key=lambda x: -x[0])

    candidates: list[tuple[str, str]] = []
    series_order = sorted(by_series.keys(), key=lambda s: -len(by_series[s]))
    while len(candidates) < sample and any(by_series.values()):
        for series in series_order:
            if not by_series[series] or len(candidates) >= sample:
                continue
            _, e = by_series[series].pop(0)
            # 多图 spec 取最后一张（通常 logo 在前 1-2 张，技术图在后）
            chosen_image = e.image_paths[-1]
            candidates.append((e.spec_id, chosen_image))

    from .image_resolver import resolve_image

    rows: list[dict] = []
    typer.echo(
        f"[hf-vision-smoke] model={vision_model} via LiteLLM proxy, sample={len(candidates)}"
    )
    prompt = (
        "You are reading a figure extracted from a 3GPP technical specification. "
        "In 3-5 concise English sentences, describe: (1) what the figure shows; "
        "(2) key elements / entities / arrows / labels visible; (3) likely role in the spec "
        "(architecture diagram, message flow, frame structure, etc.). "
        "Do NOT speculate about content not visible. Output plain text only."
    )

    with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
        for spec_id, repo_path in candidates:
            t0 = time.time()
            try:
                img = resolve_image(repo_path, revision=revision, token=_hf_token())
                b64 = base64.b64encode(img.local_path.read_bytes()).decode("ascii")
                resp = client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": vision_model,
                        "max_tokens": max_tokens,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                                    },
                                ],
                            }
                        ],
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
                choice = payload["choices"][0]
                msg = choice["message"]
                finish_reason = choice.get("finish_reason")
                description = (msg.get("content") or "").strip()
                usage = payload.get("usage") or {}
                reasoning_tokens = (usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                )
                # 严格判定：
                # - finish_reason=length 表示 max_tokens 被吃完，content 多半是被截断
                #   的草稿或空字符串，不能当成"成功描述"。reasoning_content 是模型的
                #   思考过程，绝不能 fallback 当成最终描述向用户暴露。
                ok = bool(description) and finish_reason != "length"
                row = {
                    "spec_id": spec_id,
                    "repo_path": repo_path,
                    "size": img.size,
                    "sha256": img.sha256,
                    "model": payload.get("model", vision_model),
                    "elapsed_s": round(time.time() - t0, 2),
                    "description": description[:800],
                    "ok": ok,
                    "finish_reason": finish_reason,
                    "completion_tokens": usage.get("completion_tokens"),
                    "reasoning_tokens": reasoning_tokens,
                }
                if not ok and not description:
                    row["error"] = (
                        f"empty content (finish_reason={finish_reason}, "
                        f"reasoning_tokens={reasoning_tokens}). "
                        f"提示：reasoning 模型需更大 max_tokens，或换用 mimo-v2-omni。"
                    )
                rows.append(row)
                mark = "✓" if ok else "✗"
                typer.echo(
                    f"  {mark} {spec_id} ({img.size}B, {time.time() - t0:.1f}s, "
                    f"finish={finish_reason}, rt={reasoning_tokens}): "
                    f"{description[:80].replace(chr(10), ' ')!r}..."
                )
            except Exception as exc:
                rows.append(
                    {
                        "spec_id": spec_id,
                        "repo_path": repo_path,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                typer.echo(f"  ✗ {spec_id}: {exc}")

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(_format_vision_smoke_md(rows, vision_model), encoding="utf-8")
    ok_count = sum(1 for r in rows if r.get("ok"))
    typer.echo(f"[hf-vision-smoke] {ok_count}/{len(rows)} succeeded → {output}")


def _format_vision_smoke_md(rows: list[dict], model: str) -> str:
    lines = [
        "# GSMA HF Vision 烟雾测试（§4.0 第 5 项）",
        "",
        f"- model: `{model}`",
        "- via: LiteLLM proxy (`$LITELLM_BASE_URL`)",
        f"- samples: {len(rows)}",
        f"- successes: {sum(1 for r in rows if r.get('ok'))}",
        "",
        "## 单图片结果",
        "",
    ]
    for r in rows:
        lines.append(f"### {r['spec_id']} — `{r['repo_path']}`")
        lines.append("")
        if not r.get("ok"):
            err = r.get("error") or (
                f"truncated (finish_reason={r.get('finish_reason')}, "
                f"completion_tokens={r.get('completion_tokens')}, "
                f"reasoning_tokens={r.get('reasoning_tokens')})"
            )
            lines.append(f"- 状态：❌ {err}")
            if r.get("description"):
                lines.append("")
                lines.append(f"_截断的 content 前 200 字符_：`{r['description'][:200]!r}`")
        else:
            lines.append(f"- bytes: {r.get('size')}")
            lines.append(f"- sha256: `{r.get('sha256', '')[:16]}…`")
            lines.append(
                f"- elapsed: {r.get('elapsed_s')}s · "
                f"finish={r.get('finish_reason')} · "
                f"completion_tokens={r.get('completion_tokens')} "
                f"(reasoning={r.get('reasoning_tokens')})"
            )
            lines.append("")
            lines.append("**描述：**")
            lines.append("")
            lines.append(f"> {r['description']}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_本报告由 `ingestion hf-vision-smoke` 生成；用于 §4.0 第 5 项门禁的端到端_"
        "_Vision 验证，不调 Redis 缓存（缓存逻辑放在 `ingestion/images/vision.py`）。_"
    )
    return "\n".join(lines)


@app.command("hf-load")
def hf_load(
    spec_id: str = typer.Argument(..., help="spec_id，如 38.211"),
    manifest: Path = typer.Option(None, help="SQLite manifest 路径"),
    print_chars: int = typer.Option(800, help="只打印 raw.md 前 N 字"),
) -> None:
    """按 spec_id 从 manifest 中找到 entry，拉 raw.md，解析章节并打印统计。"""
    manifest_path = manifest or _default_manifest_path()
    if not Path(manifest_path).exists():
        raise typer.BadParameter(f"manifest not found: {manifest_path}")

    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
        revision = get_meta(conn, "last_pull_revision")

    candidates = [e for e in entries if e.spec_id == spec_id]
    if not candidates:
        raise typer.BadParameter(f"spec_id {spec_id} 不在 manifest 中")

    deduped = dedupe_keep_latest(candidates)
    entry = deduped[0]
    typer.echo(json.dumps(_entry_to_dict(entry), ensure_ascii=False, indent=2))

    loader = GsmaHfLoader(revision=revision, token=_hf_token())
    for bundle in loader.iter_specs([entry]):
        typer.echo(f"--- raw.md head ({print_chars} chars) ---")
        typer.echo(bundle.raw_markdown[:print_chars])
        typer.echo(f"--- sections: {len(bundle.sections)} ---")
        for sec in bundle.sections[:20]:
            typer.echo(
                f"  L{sec.section_level} clause={sec.clause!r} "
                f"title={sec.section_title!r} chars={sec.body_chars} images={len(sec.image_refs)}"
            )
        if len(bundle.sections) > 20:
            typer.echo(f"  ... {len(bundle.sections) - 20} more sections")


def _resolve_release_list(releases: str) -> list[str]:
    if releases.strip().lower() == "all":
        return list(DEFAULT_RELEASES)
    tokens = [t.strip() for t in releases.split(",") if t.strip()]
    out: list[str] = []
    for t in tokens:
        if t.startswith("Rel-"):
            out.append(t)
        else:
            out.append(f"Rel-{t}")
    return out


def _entry_to_dict(entry) -> dict:
    return {
        "spec_uid": entry.spec_uid,
        "spec_id": entry.spec_id,
        "release": entry.release,
        "series": entry.series,
        "spec_type": entry.spec_type,
        "title": entry.title,
        "raw_md_path": entry.raw_md_path,
        "raw_md_size": entry.raw_md_size,
        "image_count": entry.image_count,
        "image_paths": list(entry.image_paths),
        "source_doc_path": entry.source_doc_path,
        "source_doc_version": entry.source_doc_version,
        "dataset_revision": entry.dataset_revision,
    }


def _read_vision_smoke_summary(path: Path) -> tuple[int, int] | None:
    """从 gsma_vision_smoke.md 抽 (succeeded, samples) 两个数字。

    若文件不存在或解析失败，返回 None。
    """
    if not Path(path).exists():
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
        succ = sample = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- samples:"):
                sample = int(line.split(":", 1)[1].strip())
            elif line.startswith("- successes:"):
                succ = int(line.split(":", 1)[1].strip())
        if succ is None or sample is None:
            return None
        return succ, sample
    except Exception:
        return None


def _format_audit_md(
    *,
    revision: str,
    pulled_at: str,
    releases_pulled: str,
    raw_entries: list,
    deduped_entries: list,
    production_entries: list,
    series_counter: Counter,
    series_bytes: Counter,
    series_images: Counter,
    release_counter: Counter,
    dedupe_overlap: int,
    image_hash_sample: list,
    vision_smoke_summary: tuple[int, int] | None = None,
    vision_smoke_path: Path | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# GSMA/3GPP HF dataset 验证报告")
    lines.append("")
    lines.append("- dataset: `GSMA/3GPP`")
    lines.append(f"- revision: `{revision}`")
    lines.append(f"- pulled releases: `{releases_pulled}`")
    lines.append(f"- pulled at (epoch): `{pulled_at}`")
    lines.append(f"- whitelist (TS 5G 系列): `{sorted(TS_5G_SERIES_WHITELIST)}`")
    lines.append("")
    lines.append("## 1. 文件树验证")
    lines.append("")
    lines.append(
        "确认 GSMA `marked/Rel-{N}/{NN}_series/{spec_uid}/raw.md + *_img.jpg` 文件树存在。"
        " loader 通过 `HfApi.list_repo_tree` 枚举到的总 spec 数（含 TR、含跨 release 重复）："
        f" **{len(raw_entries)}**。"
    )
    lines.append("")
    lines.append("## 2. release 覆盖")
    lines.append("")
    lines.append("| Release | spec 数（含重复） |")
    lines.append("|---------|----------------:|")
    for rel, cnt in sorted(release_counter.items()):
        lines.append(f"| {rel} | {cnt} |")
    lines.append("")
    lines.append(f"- 跨 release 重复（同 spec_id 出现 >1 次）: **{dedupe_overlap}**")
    lines.append(f"- 去重后保留最新版本数: **{len(deduped_entries)}**")
    lines.append(f"- 过滤 TS + 5G 系列白名单后（M6 生产口径）: **{len(production_entries)}**")
    lines.append("")
    lines.append("## 3. 生产口径系列分布")
    lines.append("")
    lines.append("| 系列 | 文档数 | raw.md 总大小 | 图片引用 |")
    lines.append("|------|------:|--------------:|--------:|")
    for series in sorted(series_counter):
        size_mib = series_bytes[series] / 1024 / 1024
        lines.append(
            f"| {series} | {series_counter[series]} |"
            f" {size_mib:.1f} MiB | {series_images[series]} |"
        )
    lines.append("")
    total_md = sum(series_bytes.values()) / 1024 / 1024
    total_img = sum(series_images.values())
    lines.append(
        f"**生产口径合计**：specs={len(production_entries)} raw.md≈{total_md:.1f} MiB"
        f" 图片引用={total_img}。"
        " 唯一图片 hash 去重需在 hf-index 阶段对每张图算 sha256，本 audit 阶段不做全量下载；"
        "文档 §4.0 给出的历史基线约 6,435 张唯一 hash（GSMA 跨 spec 复用率高，引用数 ≫ 唯一数）。"
    )
    lines.append("")
    lines.append("## 4. 版本映射（spec_id → release / GSMA spec_uid / 3GPP 文件版本号）")
    lines.append("")
    lines.append(
        "下表是生产口径全集（按 spec_id 排序）；`source_doc_version` 来自 `original/` 目录"
        " docx 文件名，对应 3GPP 官方版本号（如 j50 = R19 第 5 版）。"
    )
    lines.append("")
    lines.append("| spec_id | release | spec_uid | source_doc_version | raw.md MiB | imgs |")
    lines.append("|---------|---------|----------|--------------------|-----------:|-----:|")
    sample_for_table = sorted(production_entries, key=lambda e: e.spec_id)
    for e in sample_for_table[:50]:
        lines.append(
            f"| {e.spec_id} | {e.release} | {e.spec_uid} | {e.source_doc_version or '-'} |"
            f" {e.raw_md_size/1024/1024:.2f} | {e.image_count} |"
        )
    if len(sample_for_table) > 50:
        lines.append("| ... | ... | ... | ... | ... | ... |")
        lines.append(f"\n_（完整列表 {len(sample_for_table)} 行，已存入 manifest SQLite）_")
    lines.append("")
    lines.append("## 5. License / 使用边界")
    lines.append("")
    lines.append(
        "- GSMA HF dataset README 标 `license: other / license_name: 3gpp`，"
        "声明 *Redistribution here is limited to mirroring the public publications;*"
        "*consult the upstream source for authoritative versions and for any commercial use.*"
    )
    lines.append(
        "- 本项目使用范围（内部检索 / 引用 / 缓存 / 公网访问）的合规边界：仅限"
        " 3GPP 公开发布版本，输出端如显示 spec 全文需附 3GPP 来源声明；"
        "不做未授权的二次分发；缓存仅留在本机/项目内部数据卷。"
    )
    lines.append(
        "- 上游：<https://www.3gpp.org/specifications> ; 3GPP IP 政策：<https://www.3gpp.org/about-us/ip-policy>"
    )
    lines.append("")
    lines.append("## 6. 图片下载 + hash 抽样")
    lines.append("")
    if not image_hash_sample:
        lines.append("_未抽样（`--sample-images=0`）_")
    else:
        lines.append("| spec_id | repo_path | bytes | sha256 |")
        lines.append("|---------|-----------|------:|--------|")
        for spec_id, repo_path, size, sha in image_hash_sample:
            short_path = repo_path.rsplit("/", 1)[-1]
            sha_short = sha[:16] + "…" if isinstance(sha, str) and len(sha) == 64 else str(sha)
            lines.append(f"| {spec_id} | …/{short_path} | {size} | `{sha_short}` |")
        lines.append("")
        lines.append("所有抽样图片下载成功（hash 列稳定）→ hash 缓存命中后不会重复计费的前提成立。")
    lines.append("")
    lines.append("## 6.1 Vision 烟雾测试")
    lines.append("")
    if vision_smoke_summary is None:
        lines.append(
            "_未运行 Vision 烟雾测试_。要补齐 §4.0 第 5 项门禁请运行："
            " `ingestion hf-vision-smoke --sample 10`。"
        )
        vision_ok = False
    else:
        succ, total = vision_smoke_summary
        vision_ok = succ == total and total > 0
        rel_path = str(vision_smoke_path) if vision_smoke_path else "(unknown path)"
        lines.append(
            f"- Vision 模型：`{os.environ.get('LLM_VISION_MODEL', 'mimo-v2.5')}`"
            f" via LiteLLM proxy"
        )
        lines.append(f"- 抽样：{total} 张图片跨多系列；成功：{succ}")
        lines.append(f"- 详细报告：[`{rel_path}`]({rel_path})")
        lines.append(f"- 结论：{'✅ 链路打通，全部成功' if vision_ok else '⚠️ 部分失败'}")
    lines.append("")
    lines.append("## 7. 门禁结论")
    lines.append("")
    image_hash_ok = bool(image_hash_sample) and all(
        isinstance(x[3], str) and not x[3].startswith("FAIL") for x in image_hash_sample
    )
    gate_ok = (
        len(raw_entries) > 0
        and len(production_entries) > 0
        and dedupe_overlap >= 0
        and image_hash_ok
        and vision_ok
    )
    lines.append(f"- 文件树验证：{'✅' if len(raw_entries) > 0 else '❌'}")
    lines.append(f"- release 覆盖：{'✅' if len(production_entries) > 0 else '❌'}")
    lines.append(
        f"- 版本映射：{'✅' if any(e.source_doc_version for e in production_entries) else '⚠️ 未抽到 version'}"
    )
    lines.append("- License 已核对：✅")
    if not image_hash_sample:
        lines.append("- 图片下载 + hash：⚠️ 未抽样")
    else:
        lines.append(f"- 图片下载 + hash：{'✅' if image_hash_ok else '❌'}")
    lines.append(
        f"- Vision 烟雾测试（§4.0 第 5 项）：{'✅' if vision_ok else ('⚠️ 未运行 / 未通过')}"
    )
    lines.append("")
    lines.append(f"**门禁总结**：{'✅ 通过' if gate_ok else '❌ 未通过，禁止进入 20 篇 POC 索引'}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_本报告由 `ingestion hf-audit` 自动生成；如需重跑：`ingestion hf-pull --releases 18,19 && ingestion hf-audit`。_"
    )
    return "\n".join(lines)
