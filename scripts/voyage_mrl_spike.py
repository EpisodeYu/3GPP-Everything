"""B0: voyage-4-large MRL 等价性 spike（M1→M2 决策硬门槛）。

输入：38.331 POC 已 chunk 出的 BM25 jsonl，取前 N=100 个 chunk
实验：
  A = voyage(dimensions=2048) → 截前 1024 维 + L2 renormalize
  B = voyage(dimensions=1024) 直接调
比对：per-chunk cosine(A, B)

门槛（详见 docs/04-handoff/2026-05-16-m1-to-m2-decisions.md §1.2）：
  median ≥ 0.9995, min ≥ 0.998

成本：~100k input tokens / ~$0.012（38.331 chunk content 平均 ~600-800 char ≈ 200-300 token）。

用法：
  cd /data/3GPP-Everything
  LITELLM_BASE_URL=http://localhost:4000/v1 \
  LITELLM_API_KEY=$(grep ^LITELLM_API_KEY .env | cut -d= -f2) \
  uv run --project ingestion python scripts/voyage_mrl_spike.py \
      --input /data/tgpp/bm25/voyage/by_spec/38.331.jsonl \
      --out eval-results/m2-prep/voyage_mrl_equivalence.md \
      --sample 100
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

import httpx


def _load_chunks(path: Path, n: int) -> list[dict]:
    out: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
            if len(out) >= n:
                break
    return out


def _embed(client: httpx.Client, base_url: str, api_key: str, texts: list[str], dim: int) -> tuple[list[list[float]], int]:
    resp = client.post(
        f"{base_url.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "voyage-4-large", "input": texts, "dimensions": dim},
    )
    resp.raise_for_status()
    payload = resp.json()
    data = sorted(payload["data"], key=lambda x: x["index"])
    vectors = [list(item["embedding"]) for item in data]
    tokens = int((payload.get("usage") or {}).get("prompt_tokens") or 0)
    return vectors, tokens


def _l2_renorm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n == 0:
        return list(v)
    return [x / n for x in v]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--gate-median", type=float, default=0.9995)
    ap.add_argument("--gate-min", type=float, default=0.998)
    args = ap.parse_args()

    base_url = os.environ.get("LITELLM_BASE_URL")
    api_key = os.environ.get("LITELLM_API_KEY")
    if not base_url or not api_key:
        print("LITELLM_BASE_URL / LITELLM_API_KEY required", file=sys.stderr)
        return 2

    chunks = _load_chunks(args.input, args.sample)
    if not chunks:
        print(f"no chunks in {args.input}", file=sys.stderr)
        return 2
    texts = [c["content"] for c in chunks]
    print(f"sampled {len(texts)} chunks; mean_chars={sum(len(t) for t in texts)/len(texts):.0f}")

    vec2048: list[list[float]] = []
    vec1024: list[list[float]] = []
    tokens_a = tokens_b = 0
    t0 = time.time()
    with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
        for start in range(0, len(texts), args.batch_size):
            batch = texts[start : start + args.batch_size]
            va, ta = _embed(client, base_url, api_key, batch, dim=2048)
            vb, tb = _embed(client, base_url, api_key, batch, dim=1024)
            vec2048.extend(va)
            vec1024.extend(vb)
            tokens_a += ta
            tokens_b += tb
            print(f"  batch {start}-{start+len(batch)}: tok2048={ta} tok1024={tb}")
    elapsed = time.time() - t0

    cosines: list[float] = []
    for v2048, v1024 in zip(vec2048, vec1024, strict=True):
        v_trunc = _l2_renorm(v2048[:1024])
        cosines.append(_cos(v_trunc, v1024))

    cmin = min(cosines)
    cmax = max(cosines)
    cmed = statistics.median(cosines)
    cmean = statistics.mean(cosines)
    cp01 = sorted(cosines)[max(0, len(cosines) // 100)]
    cp05 = sorted(cosines)[max(0, len(cosines) * 5 // 100)]

    # 同 chunk 双向量 norm 验证
    norms_a = [math.sqrt(sum(x * x for x in v)) for v in vec2048]
    norms_b = [math.sqrt(sum(x * x for x in v)) for v in vec1024]

    pass_median = cmed >= args.gate_median
    pass_min = cmin >= args.gate_min
    verdict = "PASS" if (pass_median and pass_min) else "FAIL"

    # 决策结论摘要
    decision = (
        "MRL 等价性成立 → 进入 B1-B4：客户端 truncate(2048→1024)+L2 renorm 即可；"
        "embed 一次产 2 维度向量，token 不翻倍。"
        if verdict == "PASS"
        else (
            "MRL 等价性 **不成立** → 暂停 B1-B4 实施。需要回到 §5 风险表第 1 行与人重谈："
            "(a) 接受 1024 单维度跑 + 放弃 2048；(b) 接受双调 API + token 翻倍 "
            "（≈290M vs 200M 额度）→ 重新评估额度方案。"
        )
    )

    md = f"""# B0: voyage-4-large MRL 等价性 spike

> 任务：`docs/04-handoff/2026-05-16-m1-to-m2-decisions.md §4 Task B0`
> 输入：`{args.input}` 取前 {len(chunks)} 个 chunk（38.331 POC chunker 输出，2026-05-15 落地）
> 模型：`voyage-4-large`（LiteLLM proxy，`dimensions` 参数双调）

## 1. 实验设置

- A = voyage(dimensions=2048) → 客户端取前 1024 维 + L2 renormalize
- B = voyage(dimensions=1024) 直接调
- 指标：per-chunk cosine(A, B)（两向量都是 unit-norm，所以 cosine = dot product）

## 2. 统计摘要

| 指标 | 值 |
|---|---|
| 样本数 | {len(cosines)} |
| cosine min | **{cmin:.6f}** |
| cosine 5th pct | {cp05:.6f} |
| cosine 1st pct | {cp01:.6f} |
| cosine median | **{cmed:.6f}** |
| cosine mean | {cmean:.6f} |
| cosine max | {cmax:.6f} |
| 2048 向量 norm 均值 | {sum(norms_a)/len(norms_a):.6f} |
| 1024 直调向量 norm 均值 | {sum(norms_b)/len(norms_b):.6f} |
| 双调总 input tokens | A={tokens_a} + B={tokens_b} = {tokens_a+tokens_b} |
| 估算成本（$0.12/M）| ${(tokens_a+tokens_b) * 0.12 / 1_000_000:.4f} |
| 端到端耗时 | {elapsed:.1f}s |

## 3. 门槛

| 项 | 门槛 | 实测 | 是否通过 |
|---|---|---|---|
| cosine median | ≥ {args.gate_median} | {cmed:.6f} | {'✅' if pass_median else '❌'} |
| cosine min | ≥ {args.gate_min} | {cmin:.6f} | {'✅' if pass_min else '❌'} |

## 4. 决策

**verdict = {verdict}**

{decision}

---

_本报告由 `scripts/voyage_mrl_spike.py` 自动生成（M1→M2 过渡 Task B0）。_
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md)
    print(f"\n=== {verdict} ===")
    print(f"median={cmed:.6f} min={cmin:.6f} mean={cmean:.6f}")
    print(f"tokens={tokens_a+tokens_b} cost=${(tokens_a+tokens_b)*0.12/1_000_000:.4f} elapsed={elapsed:.1f}s")
    print(f"wrote {args.out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
