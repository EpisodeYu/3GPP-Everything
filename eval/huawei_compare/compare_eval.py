"""对比评测编排：results.json + golden → scores.json（README §8.2）。

三类打分（裁判 glm-5.1，与 A=mimo / B=gpt-4o-mini / C=deepseek 都不同源）：
1. **绝对**（每系统各打）：fact_coverage（positive，复用 `FactCoverageJudge`）+ negative 拒答
   （`NegativeJudge`）。
2. **检索专项（LLM-free）**：fact-in-context recall（expected_facts 是否出现在检索 context）
   + spec 归属命中（expected_spec ∈ 系统 cited_specs）+ 利用率（coverage ÷ recall）。
3. **成对盲评**（`PairwiseJudge` + 位置对冲）：A-B / A-C / B-C 各正反两序。

LLM 调用用线程池并发（langchain `.invoke` 是阻塞的；judge 已单题异常隔离）。

用法（eval venv）：
    PYTHONPATH=/data/3GPP-Everything eval/.venv/bin/python -m eval.huawei_compare.compare_eval \
        --golden eval/huawei_compare/golden_compare.yaml \
        --results eval-results/huawei-compare/results.json \
        --out eval-results/huawei-compare/scores.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from eval.huawei_compare.pairwise_judge import aggregate_pair, build_pairwise_judge
from eval.runner_retrieval import GoldenItem, load_golden

log = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL = "glm-5.1"
DEFAULT_WORKERS = 6
RETRIEVAL_SYSTEMS = frozenset({"A", "B"})  # 有检索 context 的系统（C 无）

_NONWORD_RE = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class _Resp:
    """喂给 FactCoverageJudge / NegativeJudge 的最小 resp（只用 .answer）。"""

    answer: str


# === 检索专项指标（LLM-free）=============================================


def _fact_in_text(fact: str, norm_text: str) -> bool:
    """fact 是否出现在已规范化文本里（>=4 字符显著词 60% 命中 / 全短词则整串子串）。"""
    words = [w for w in _NONWORD_RE.sub(" ", fact.lower()).split() if len(w) >= 4]
    if not words:
        return _NONWORD_RE.sub(" ", fact.lower()).strip() in norm_text
    hit = sum(1 for w in words if w in norm_text)
    return hit / len(words) >= 0.6


def fact_in_context_recall(expected_facts: list[str], contexts: list[str]) -> float | None:
    """expected_facts 有多少出现在检索回来的 context 里（纯子串，零 LLM）。无 context → None。"""
    facts = [f for f in (expected_facts or []) if f and f.strip()]
    if not facts:
        return None
    if not contexts:
        return None
    norm = _NONWORD_RE.sub(" ", "\n".join(contexts).lower())
    return sum(1 for f in facts if _fact_in_text(f, norm)) / len(facts)


def spec_recall(expected_specs: list[str], cited_specs: list[str]) -> bool | None:
    """期望 spec 是否 ∈ 系统引用/检索到的 spec 集（命中率口径）。无期望 spec → None。"""
    exp = {s for s in (expected_specs or []) if s}
    if not exp:
        return None
    return bool(exp & set(cited_specs or []))


# === 绝对打分 =============================================================


def _score_one_system(item: GoldenItem, sa: dict | None, fc_judge: Any, neg_judge: Any) -> dict:
    """单题单系统：fact_coverage / negative / 检索专项。sa = SystemAnswer-dict 或 None。"""
    if sa is None:
        return {"present": False}
    answer = str(sa.get("answer") or "")
    resp = _Resp(answer=answer)
    ok = sa.get("error") is None and bool(answer.strip())
    out: dict[str, Any] = {
        "present": True,
        "ok": ok,
        "system": sa.get("system"),
        "elapsed_ms": int(sa.get("elapsed_ms") or 0),
    }
    exp_specs = [e.spec_id for e in item.expected_specs]
    out["spec_recall"] = spec_recall(exp_specs, sa.get("cited_specs") or [])

    if item.must_say_not_found:
        out["negative_verdict"] = neg_judge.score_item(item, resp).get("verdict")
    else:
        fc = fc_judge.score_item(item, resp)
        out["fact_coverage"] = fc.get("score")
        recall = fact_in_context_recall(item.expected_facts, list(sa.get("contexts") or []))
        out["fact_in_context_recall"] = recall
        # 利用率：检索到的料里有多少最终被答出来（coverage ÷ recall），仅当 recall>0
        if fc.get("score") is not None and recall not in (None, 0):
            out["utilization"] = round(min(1.0, fc["score"] / recall), 3)
    return out


def score_absolute(
    items: list[GoldenItem],
    results: dict,
    *,
    fc_judge: Any,
    neg_judge: Any,
    systems: list[str],
    workers: int = DEFAULT_WORKERS,
) -> list[dict]:
    """每题每系统绝对打分（线程池并发）。返回 per-item 列表。"""
    items_by_id = {it.id: it for it in items}
    res_by_id = {it["item_id"]: it for it in results["items"]}

    jobs: list[tuple[str, str]] = []  # (item_id, system)
    for iid in items_by_id:
        if iid in res_by_id:
            jobs.extend((iid, s) for s in systems)

    def _run(job: tuple[str, str]) -> tuple[str, str, dict]:
        iid, sys = job
        return (
            iid,
            sys,
            _score_one_system(items_by_id[iid], res_by_id[iid].get(sys), fc_judge, neg_judge),
        )

    scored: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n, (iid, sys, r) in enumerate(ex.map(_run, jobs), 1):
            scored.setdefault(iid, {})[sys] = r
            if n % 50 == 0:
                log.info("absolute %d/%d", n, len(jobs))

    out: list[dict] = []
    for iid, it in items_by_id.items():
        if iid not in scored:
            continue
        out.append(
            {
                "item_id": iid,
                "category": it.category,
                "question": it.question,
                "expected_specs": [e.spec_id for e in it.expected_specs],
                "per_system": scored[iid],
            }
        )
    return out


# === 成对盲评 =============================================================


def score_pairwise(
    items: list[GoldenItem],
    results: dict,
    *,
    pw_judge: Any,
    systems: list[str],
    workers: int = DEFAULT_WORKERS,
) -> dict[str, list[dict]]:
    """A-B / A-C / B-C 各题正反两序 → 位置对冲胜者。返回 {pair_key: [{item_id, winner, ...}]}。"""
    items_by_id = {it.id: it for it in items}
    res_by_id = {it["item_id"]: it for it in results["items"]}
    pairs = list(combinations(systems, 2))

    def _ans(iid: str, sys: str) -> str:
        sa = res_by_id[iid].get(sys) or {}
        return str(sa.get("answer") or "")

    # 每个 (pair, item) 两序两次调用
    jobs: list[tuple[tuple[str, str], str]] = [
        (pair, iid) for pair in pairs for iid in items_by_id if iid in res_by_id
    ]

    def _run(job: tuple[tuple[str, str], str]) -> tuple[str, str, dict]:
        (s1, s2), iid = job
        item = items_by_id[iid]
        v_ab = pw_judge.score_pair(item, _ans(iid, s1), _ans(iid, s2)).get("verdict")  # s1=甲
        v_ba = pw_judge.score_pair(item, _ans(iid, s2), _ans(iid, s1)).get("verdict")  # s2=甲
        winner_idx = aggregate_pair(v_ab, v_ba)  # '1'|'2'|'TIE'
        winner = {"1": s1, "2": s2, "TIE": "TIE"}[winner_idx]
        return f"{s1}_vs_{s2}", iid, {"item_id": iid, "winner": winner, "v_ab": v_ab, "v_ba": v_ba}

    out: dict[str, list[dict]] = {f"{a}_vs_{b}": [] for a, b in pairs}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n, (key, _iid, rec) in enumerate(ex.map(_run, jobs), 1):
            out[key].append(rec)
            if n % 50 == 0:
                log.info("pairwise %d/%d", n, len(jobs))
    return out


# === 编排 =================================================================


def build_judges(model: str = DEFAULT_JUDGE_MODEL) -> tuple[Any, Any, Any]:
    """构建 fact_coverage / negative / pairwise 三个 judge，统一用中立 model（glm-5.1）。"""
    from langchain_openai import ChatOpenAI

    from eval.fact_coverage_judge import FactCoverageJudge
    from eval.negative_judge import NegativeJudge
    from eval.settings import get_settings

    s = get_settings()
    if not s.litellm_api_key:
        raise RuntimeError("LITELLM_API_KEY missing in env/.env")
    llm = ChatOpenAI(
        model=model,
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        temperature=0.01,
    )
    return FactCoverageJudge(llm=llm), NegativeJudge(llm=llm), build_pairwise_judge(model=model)


def run(
    golden_path: Path,
    results_path: Path,
    *,
    model: str = DEFAULT_JUDGE_MODEL,
    do_pairwise: bool = True,
    workers: int = DEFAULT_WORKERS,
) -> dict:
    items = load_golden(golden_path)
    results = json.loads(results_path.read_text(encoding="utf-8"))
    systems = list(results.get("systems") or ["A", "B"])
    fc_judge, neg_judge, pw_judge = build_judges(model)

    log.info("absolute scoring: %d items × %d systems", len(items), len(systems))
    per_item = score_absolute(
        items, results, fc_judge=fc_judge, neg_judge=neg_judge, systems=systems, workers=workers
    )
    pairwise: dict[str, list[dict]] = {}
    if do_pairwise and len(systems) >= 2:
        log.info("pairwise scoring (位置对冲)")
        pairwise = score_pairwise(
            items, results, pw_judge=pw_judge, systems=systems, workers=workers
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "systems": systems,
        "judge_model": model,
        "n_items": len(per_item),
        "items": per_item,
        "pairwise": pairwise,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    ap.add_argument("--no-pairwise", action="store_true")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = ap.parse_args()

    scores = run(
        args.golden,
        args.results,
        model=args.model,
        do_pairwise=not args.no_pairwise,
        workers=args.workers,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"scores → {args.out}（{scores['n_items']} 题, "
        f"systems={scores['systems']}, judge={args.model}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
