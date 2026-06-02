"""B = 华为 Telco-RAG 采集器（**在 telco venv 跑，不是 eval venv**）。

本机 harness 不允许常驻后台服务（uvicorn 被杀），故不走 HTTP，而是 **in-process**
直接调 `api.pipeline.TelcoRAG()`。本脚本刻意只用 stdlib + Telco-RAG 自身代码，
**不 import 本仓库 eval 包**（telco venv 里没有它）。写最朴素 raw JSONL，
contexts 切分 / cited_specs 抽取留给 eval venv 的 `merge_results.py`（共享解析 + 有单测）。

用法（telco venv）：
    OPENAI_BASE_URL="https://api.apiyi.com/v1" OPENAI_API_KEY="<relay key>" \
    /data/telco-rag/.venv/bin/python \
      /data/3GPP-Everything/eval/huawei_compare/collect_b.py \
      --in /data/3GPP-Everything/eval/huawei_compare/smoke_questions.jsonl \
      --out /data/3GPP-Everything/eval-results/huawei-compare-smoke/b_raw.jsonl

env：OPENAI_API_KEY 必填；OPENAI_BASE_URL 走中转；TELCORAG_MODEL 默认 gpt-4o-mini。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_TELCO_ROOT = "/data/telco-rag/Telco-RAG_api"


def _bootstrap_telco(telco_root: str) -> None:
    """chdir 到 Telco-RAG_api（其代码用相对路径 ./3GPP-Release18 等）+ 上 sys.path。"""
    os.chdir(telco_root)
    if telco_root not in sys.path:
        sys.path.insert(0, telco_root)
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


def _read_questions(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            iid = str(rec.get("item_id") or "").strip()
            q = str(rec.get("question") or "").strip()
            if iid and q:
                out.append({"item_id": iid, "question": q})
    return out


async def _collect(questions: list[dict], *, model: str, api_key: str, out_path: str) -> int:
    from api.pipeline import TelcoRAG

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    ok = 0
    # 增量落盘：单题跑完即写一行，长批次中途崩了也不丢已采集的
    with open(out_path, "w", encoding="utf-8") as f:
        for i, q in enumerate(questions, start=1):
            iid, question = q["item_id"], q["question"]
            rec: dict = {"item_id": iid, "question": question, "system": "B", "model": model}
            t0 = time.time()
            try:
                result = await TelcoRAG(query=question, model_name=model, api_key=api_key)
                if result:
                    resp, ctx, rephrased = result
                    rec.update(
                        answer=resp or "",
                        retrieval_raw=ctx or "",
                        rephrased_query=rephrased or "",
                        error=None,
                    )
                    if (resp or "").strip():
                        ok += 1
                else:
                    rec.update(answer="", retrieval_raw="", error={"type": "NoneResult"})
            except Exception as exc:  # 单题隔离
                rec.update(
                    answer="",
                    retrieval_raw="",
                    error={"type": type(exc).__name__, "detail": str(exc)[:500]},
                )
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print(f"B [{i}/{len(questions)}] {iid} ok={rec.get('error') is None}", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", dest="outfile", required=True)
    ap.add_argument("--telco-root", default=DEFAULT_TELCO_ROOT)
    ap.add_argument("--model", default=os.environ.get("TELCORAG_MODEL", "gpt-4o-mini"))
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        ap.error("缺 OPENAI_API_KEY 环境变量（Telco-RAG 检索向量 + LLM 都走它）")

    infile = os.path.abspath(args.infile)
    outfile = os.path.abspath(args.outfile)
    _bootstrap_telco(args.telco_root)

    questions = _read_questions(infile)
    ok = asyncio.run(_collect(questions, model=args.model, api_key=api_key, out_path=outfile))
    print(f"B 采集完成：{len(questions)} 题写入 {outfile}（ok={ok}/{len(questions)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
