"""Vision model 对比 benchmark。

跑 (mimo-v2.5 / mimo-v2-omni) × (max_tokens=4096 / 8192) × 同一组 N 张图，
记录 finish_reason / completion_tokens / reasoning_tokens / 耗时 / 描述前 200 字符，
最后输出 markdown 报告供人审。

不是产线代码；放在 scripts/ 下，需要时手动跑。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huggingface_hub import hf_hub_download  # noqa: E402

from ingestion.hf_loader import (  # noqa: E402
    dedupe_keep_latest,
    filter_ts_5g,
    manifest_session,
    read_entries,
    get_meta,
)


PROMPT = (
    "You are reading a figure extracted from a 3GPP technical specification. "
    "In 3-5 concise English sentences, describe: (1) what the figure shows; "
    "(2) key elements / entities / arrows / labels visible; (3) likely role in the spec "
    "(architecture diagram, message flow, frame structure, etc.). "
    "Do NOT speculate about content not visible. Output plain text only."
)


@dataclass
class Result:
    model: str
    max_tokens: int
    spec_id: str
    image_path: str
    image_bytes: int
    finish_reason: str | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    elapsed_s: float
    content_len: int
    content_head: str
    ok: bool
    error: str | None = None


def call_vision(
    model: str,
    image_bytes: bytes,
    max_tokens: int,
    base_url: str,
    api_key: str,
    timeout: float = 240.0,
) -> tuple[dict, float]:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    t0 = time.time()
    resp = httpx.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
        },
        timeout=timeout,
    )
    elapsed = time.time() - t0
    resp.raise_for_status()
    return resp.json(), elapsed


def pick_samples(manifest_path: str, n: int) -> list[tuple[str, str]]:
    """与 hf-vision-smoke 同一份抽样策略，保证不同组合用同一组图。"""
    with manifest_session(manifest_path) as conn:
        entries = read_entries(conn)
    production = filter_ts_5g(dedupe_keep_latest(entries))
    by_series: dict[str, list] = {}
    for e in production:
        if not e.image_paths:
            continue
        score = 1 if len(e.image_paths) >= 2 else 0
        by_series.setdefault(e.series, []).append((score, e))
    for s in by_series.values():
        s.sort(key=lambda x: -x[0])
    order = sorted(by_series.keys(), key=lambda k: -len(by_series[k]))
    out: list[tuple[str, str]] = []
    while len(out) < n and any(by_series.values()):
        for series in order:
            if not by_series[series] or len(out) >= n:
                continue
            _, e = by_series[series].pop(0)
            out.append((e.spec_id, e.image_paths[-1]))
    return out


def main() -> None:
    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    hf_token = os.environ.get("HF_TOKEN")
    if not base_url or not api_key:
        print("ERROR: set LITELLM_BASE_URL and LITELLM_API_KEY", file=sys.stderr)
        sys.exit(2)

    manifest_path = os.environ.get("INGEST_DATA_DIR", "/data/tgpp") + "/markdown/gsma_manifest.sqlite"
    samples = pick_samples(manifest_path, n=10)
    with manifest_session(manifest_path) as conn:
        revision = get_meta(conn, "last_pull_revision")
    print(f"# samples: {len(samples)}, revision={revision[:12]}")

    # 预下载所有图片到本地缓存
    images: dict[str, tuple[bytes, int]] = {}
    for spec_id, path in samples:
        local = hf_hub_download(
            "GSMA/3GPP",
            path,
            repo_type="dataset",
            revision=revision,
            token=hf_token,
        )
        b = Path(local).read_bytes()
        images[spec_id] = (b, len(b))

    matrix = [
        ("mimo-v2.5", 4096),
        ("mimo-v2.5", 8192),
        ("mimo-v2-omni", 4096),
        ("mimo-v2-omni", 8192),
    ]

    results: list[Result] = []
    for model, mt in matrix:
        print(f"\n== {model} max_tokens={mt} ==")
        for spec_id, path in samples:
            img_b, size = images[spec_id]
            try:
                payload, elapsed = call_vision(model, img_b, mt, base_url, api_key)
                choice = payload["choices"][0]
                msg = choice["message"]
                usage = payload.get("usage") or {}
                rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
                content = (msg.get("content") or "").strip()
                finish = choice.get("finish_reason")
                r = Result(
                    model=model,
                    max_tokens=mt,
                    spec_id=spec_id,
                    image_path=path,
                    image_bytes=size,
                    finish_reason=finish,
                    completion_tokens=usage.get("completion_tokens"),
                    reasoning_tokens=rt,
                    elapsed_s=round(elapsed, 2),
                    content_len=len(content),
                    content_head=content[:200],
                    ok=bool(content) and finish != "length",
                )
                mark = "✓" if r.ok else "✗"
                print(
                    f"  {mark} {spec_id:>10s}: finish={finish:>7s} ct={r.completion_tokens:>4} "
                    f"rt={rt} elapsed={elapsed:.1f}s len={r.content_len}"
                )
            except Exception as exc:
                r = Result(
                    model=model,
                    max_tokens=mt,
                    spec_id=spec_id,
                    image_path=path,
                    image_bytes=size,
                    finish_reason=None,
                    completion_tokens=None,
                    reasoning_tokens=None,
                    elapsed_s=0.0,
                    content_len=0,
                    content_head="",
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                print(f"  EXC {spec_id}: {exc}")
            results.append(r)

    # 写报告
    out_path = Path("eval-results/source-audit/vision_model_benchmark.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(results, samples, out_path)
    print(f"\n→ wrote {out_path}")

    # JSON 兜底，方便后续脚本读
    json_path = out_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            [
                {k: v for k, v in r.__dict__.items() if not isinstance(v, bytes)}
                for r in results
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


def write_report(results: list[Result], samples: list[tuple[str, str]], out_path: Path) -> None:
    by_combo: dict[tuple[str, int], list[Result]] = {}
    for r in results:
        by_combo.setdefault((r.model, r.max_tokens), []).append(r)

    lines: list[str] = []
    lines.append("# Vision model 对比 benchmark")
    lines.append("")
    lines.append(f"- samples: {len(samples)} 张跨多系列；同一组图用于所有组合")
    lines.append("- 评估指标：成功率 / 平均 completion_tokens / 平均 reasoning_tokens / 平均耗时 / 描述质量（人审）")
    lines.append("")
    lines.append("## 1. 汇总指标")
    lines.append("")
    lines.append(
        "| 模型 | max_tokens | 成功率 | 平均 ct | 平均 rt | 平均 content_len | 平均耗时 (s) |"
    )
    lines.append("|------|-----------:|------:|--------:|--------:|-----------------:|-------------:|")
    for (model, mt), rs in by_combo.items():
        ok = sum(1 for r in rs if r.ok)
        ct = [r.completion_tokens for r in rs if r.completion_tokens]
        rt = [r.reasoning_tokens for r in rs if r.reasoning_tokens]
        cl = [r.content_len for r in rs if r.ok]
        elapsed = [r.elapsed_s for r in rs if r.ok]
        lines.append(
            f"| `{model}` | {mt} | {ok}/{len(rs)} | "
            f"{int(mean(ct)) if ct else 0} | {int(mean(rt)) if rt else 0} | "
            f"{int(mean(cl)) if cl else 0} | {round(mean(elapsed), 1) if elapsed else 0} |"
        )
    lines.append("")
    lines.append("## 2. 按图片细节（每张图四种组合并排）")
    lines.append("")
    spec_to_idx = {sid: i for i, (sid, _) in enumerate(samples)}
    for spec_id, path in samples:
        rs = [r for r in results if r.spec_id == spec_id]
        lines.append(f"### {spec_id} — `{path.rsplit('/', 1)[-1]}` ({rs[0].image_bytes}B)")
        lines.append("")
        lines.append("| 模型 | max_tokens | finish | ct | rt | 耗时 (s) | 描述前 200 字符 |")
        lines.append("|------|-----------:|--------|----:|----:|--------:|--------------|")
        for r in rs:
            mark = "✓" if r.ok else "❌"
            head = r.content_head.replace("\n", " ")[:200]
            lines.append(
                f"| `{r.model}` | {r.max_tokens} | {mark}`{r.finish_reason}` | "
                f"{r.completion_tokens or '-'} | {r.reasoning_tokens or '-'} | "
                f"{r.elapsed_s} | {head} |"
            )
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
