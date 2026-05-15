"""Vision pipeline CLI 子命令。

子命令：
- vision-call    单张图片调一次 mimo（debug / 数据点抽检用），打印结构化结果
- vision-cache   查 Redis 缓存：单图状态 / dead-letter 列表 / 清理

被 ingestion/cli.py 在顶层 typer app 上挂载。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import typer

from ingestion.hf_loader.manifest_store import get_meta, manifest_session

from .vision import (
    CACHE_KEY_DEAD,
    CACHE_KEY_OK,
    CACHE_KEY_RETRY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TOKENS,
    VisionResolver,
    _LiteLLMClient,
    _VisionCache,
    make_default_image_loader,
    vision_result_to_json,
)

app = typer.Typer(no_args_is_help=True, help="3GPP-Everything vision pipeline CLI")
log = logging.getLogger(__name__)


def _default_manifest_path() -> Path:
    base = os.environ.get("INGEST_DATA_DIR") or "/data/tgpp"
    return Path(base) / "markdown" / "gsma_manifest.sqlite"


def _resolve_revision_from_manifest() -> str | None:
    p = _default_manifest_path()
    if not p.exists():
        return None
    with manifest_session(p) as conn:
        return get_meta(conn, "last_pull_revision")


@app.command("vision-call")
def vision_call(
    image_path: str = typer.Argument(
        ..., help="HF repo path (marked/Rel-19/.../*.jpg) 或本地文件路径"
    ),
    spec_id: str = typer.Option("", help="ctx.spec_id；纯 debug 时可留空"),
    clause: str = typer.Option("", help="ctx.clause"),
    section_title: str = typer.Option("", help="ctx.section_title"),
    no_cache: bool = typer.Option(
        False, help="禁用 Redis 缓存（强制重新调 mimo）"
    ),
    max_tokens: int = typer.Option(DEFAULT_MAX_TOKENS, help="单次响应 max_tokens"),
    max_retries: int = typer.Option(
        DEFAULT_MAX_RETRIES, help="失败重试次数（生产值；debug 可降为 0 快速失败）"
    ),
    print_full: bool = typer.Option(False, help="完整打印 description（默认截 800 字符）"),
    log_level: str = typer.Option("INFO"),
) -> None:
    """对单张图片调一次 mimo vision，打印结构化结果（含 description / labels / acronyms）。

    生产路径上 chunker 会通过 `VisionResolver` 自动调用本同名 API；本命令是
    人审 / 抽检 / 调试用。结果**会写 Redis 缓存**（除非 --no-cache），
    所以同图重复调试不会重复计费。
    """
    logging.basicConfig(level=log_level)

    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    if not base_url or not api_key:
        raise typer.BadParameter("LITELLM_BASE_URL / LITELLM_API_KEY 必须在 .env 中提供")

    revision = _resolve_revision_from_manifest()

    cache: _VisionCache | None
    if no_cache:
        cache = _VisionCache(redis_client=None)
        cache._client = None  # type: ignore[attr-defined]
    else:
        cache = None  # 让 VisionResolver 自连 REDIS_URL

    with _LiteLLMClient(base_url=base_url, api_key=api_key) as http:
        resolver = VisionResolver(
            http_client=http,
            cache=cache if cache is not None else _VisionCache(),
            image_loader=make_default_image_loader(
                revision=revision, token=os.environ.get("HF_TOKEN") or None
            ),
            model=os.environ.get("LLM_VISION_MODEL") or "mimo-v2.5",
            max_tokens=max_tokens,
            max_retries=max_retries,
        )

        ctx = {
            "spec_id": spec_id,
            "clause": clause,
            "section_title": section_title,
        }
        out = resolver(image_path, ctx)

    if out is None:
        typer.echo("[vision-call] ❌ resolver returned None (dead-letter or image load fail)")
        raise typer.Exit(code=1)

    typer.echo(
        f"[vision-call] ✅ {'cached' if out.get('cached') else 'fresh'} "
        f"figure_kind={out.get('figure_kind')} "
        f"ct={out.get('completion_tokens')} rt={out.get('reasoning_tokens')}"
    )
    desc = out.get("description") or ""
    if not print_full and len(desc) > 800:
        desc = desc[:800] + "..."
    typer.echo("\n--- description ---")
    typer.echo(desc)
    typer.echo("")
    typer.echo(f"spec_role: {out.get('spec_role')}")
    if out.get("undescribable_reason"):
        typer.echo(f"undescribable_reason: {out.get('undescribable_reason')}")
    typer.echo(f"\nvisible_labels ({len(out.get('visible_labels') or [])}):")
    for label in (out.get("visible_labels") or [])[:20]:
        typer.echo(f"  - {label}")
    typer.echo(f"\nvisible_acronyms ({len(out.get('visible_acronyms') or [])}):")
    for acr in (out.get("visible_acronyms") or [])[:20]:
        typer.echo(f"  - {acr}")


@app.command("vision-cache")
def vision_cache(
    sha256: str = typer.Option(
        "", help="单 hash 查询：列出 OK / RETRY / DEAD 三个 key 状态"
    ),
    list_dead: bool = typer.Option(False, help="列出所有 dead-letter 条目"),
    list_retry: bool = typer.Option(False, help="列出所有 retry 队列条目"),
    purge_dead: bool = typer.Option(False, help="清空 dead-letter 队列（不影响 OK 缓存）"),
    purge_retry: bool = typer.Option(False, help="清空 retry 队列"),
) -> None:
    """检查 / 清理 Vision Redis 缓存。

    生产场景：
    - 全量索引前后跑 `--list-dead` 看是否有图片需要人工修复
    - 修复后 `--purge-dead` 让下次 chunk/index 重新尝试
    """
    cache = _VisionCache()
    if not cache.enabled:
        raise typer.BadParameter("REDIS_URL 未配置或连接失败；缓存禁用")
    client = cache._client  # type: ignore[attr-defined]

    if sha256:
        ok = client.get(CACHE_KEY_OK.format(sha256=sha256))
        retry = client.get(CACHE_KEY_RETRY.format(sha256=sha256))
        dead = client.get(CACHE_KEY_DEAD.format(sha256=sha256))
        typer.echo(f"sha256: {sha256}")
        typer.echo(f"OK   : {'present' if ok else 'absent'}")
        if ok:
            typer.echo("       " + (json.dumps(json.loads(ok), ensure_ascii=False)[:200]) + "...")
        typer.echo(f"RETRY: {'present' if retry else 'absent'}")
        if retry:
            typer.echo("       " + retry[:300])
        typer.echo(f"DEAD : {'present' if dead else 'absent'}")
        if dead:
            typer.echo("       " + dead[:300])

    if list_dead:
        keys = list(client.scan_iter(match="tgpp:vision:dead:*"))
        typer.echo(f"\n[dead-letter] {len(keys)} entries:")
        for k in keys[:50]:
            v = client.get(k)
            typer.echo(f"  {k}")
            if v:
                typer.echo(f"    {v[:200]}")
        if len(keys) > 50:
            typer.echo(f"  ... {len(keys) - 50} more")

    if list_retry:
        keys = list(client.scan_iter(match="tgpp:vision:retry:*"))
        typer.echo(f"\n[retry-queue] {len(keys)} entries:")
        for k in keys[:50]:
            v = client.get(k)
            typer.echo(f"  {k}")
            if v:
                typer.echo(f"    {v[:200]}")
        if len(keys) > 50:
            typer.echo(f"  ... {len(keys) - 50} more")

    if purge_dead:
        keys = list(client.scan_iter(match="tgpp:vision:dead:*"))
        if keys:
            client.delete(*keys)
        typer.echo(f"[purge-dead] removed {len(keys)} entries")

    if purge_retry:
        keys = list(client.scan_iter(match="tgpp:vision:retry:*"))
        if keys:
            client.delete(*keys)
        typer.echo(f"[purge-retry] removed {len(keys)} entries")

    if not any([sha256, list_dead, list_retry, purge_dead, purge_retry]):
        # 默认行为：summary
        ok_keys = sum(1 for _ in client.scan_iter(match="tgpp:vision:[!rd]*"))
        retry_keys = sum(1 for _ in client.scan_iter(match="tgpp:vision:retry:*"))
        dead_keys = sum(1 for _ in client.scan_iter(match="tgpp:vision:dead:*"))
        typer.echo("Vision cache summary:")
        typer.echo(f"  OK    : {ok_keys}")
        typer.echo(f"  RETRY : {retry_keys}")
        typer.echo(f"  DEAD  : {dead_keys}")
        typer.echo("\n用 --sha256 / --list-dead / --list-retry / --purge-dead 等查看与清理")


# 给 vision-call 输出 raw 字典（便于 piping 到 jq）—— 单独一个命令更清晰
@app.command("vision-call-json")
def vision_call_json(
    image_path: str = typer.Argument(..., help="HF repo path 或本地文件路径"),
    spec_id: str = typer.Option(""),
    clause: str = typer.Option(""),
    section_title: str = typer.Option(""),
    no_cache: bool = typer.Option(False),
    include_raw: bool = typer.Option(False, help="dump 含 raw_response 的完整 dict"),
) -> None:
    """对单张图片调一次 mimo vision，输出 JSON（不打印人类友好排版）。便于脚本调用。"""
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    if not base_url or not api_key:
        raise typer.BadParameter("LITELLM_BASE_URL / LITELLM_API_KEY 必须在 .env 中提供")
    revision = _resolve_revision_from_manifest()
    cache = _VisionCache(redis_client=None) if no_cache else _VisionCache()
    if no_cache:
        cache._client = None  # type: ignore[attr-defined]
    with _LiteLLMClient(base_url=base_url, api_key=api_key) as http:
        resolver = VisionResolver(
            http_client=http,
            cache=cache,
            image_loader=make_default_image_loader(
                revision=revision, token=os.environ.get("HF_TOKEN") or None
            ),
        )
        out = resolver(
            image_path,
            {"spec_id": spec_id, "clause": clause, "section_title": section_title},
        )
    if out is None:
        typer.echo("null")
        raise typer.Exit(code=1)
    if not include_raw:
        out = {k: v for k, v in out.items() if k != "raw_response"}
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))


__all__ = ["app", "vision_result_to_json"]
