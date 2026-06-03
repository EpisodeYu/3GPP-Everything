"""对比测试统一数据层：A / B 两系统答案的共享 record + 对齐逻辑。

两个系统在**不同 venv** 采集（A 在 eval venv 走 HTTP；B 在 telco venv 走 in-process），
各自写 JSONL，最后由 `merge_results.py` 在 eval venv 里：
- 把 B 的精简 raw（answer + retrieval_raw）解析成 `SystemAnswer`（切 contexts + 抽 cited_specs）
- 按 `item_id` 对齐 A、B → 统一 `results.json`

把"切 contexts / 抽 spec"这类解析放这里（eval venv 有单测覆盖），collect_b 只写最朴素 raw，
避免在 telco venv 里重复实现 + 难测。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from eval.huawei_compare.build_intersection import normalize_spec_id
from eval.huawei_compare.telcorag_client import _split_contexts

SYSTEM_A = "A"  # 3GPP-Everything（本项目）
SYSTEM_B = "B"  # 华为 Telco-RAG

# Telco-RAG 检索文本里每段尾部："This retrieval is performed from the document 3GPP 29272-i20.docx."
_B_DOC_RE = re.compile(r"document\s+3GPP\s+([0-9][\w.\-]+\.docx)", re.IGNORECASE)


@dataclass(slots=True)
class SystemAnswer:
    """单题在单系统上的采集结果（A 或 B）；下游 judge 的输入。"""

    item_id: str
    question: str
    system: str  # SYSTEM_A | SYSTEM_B
    answer: str = ""
    contexts: list[str] = field(default_factory=list)  # 检索上下文文本（faithfulness judge 用）
    cited_specs: list[str] = field(default_factory=list)  # 引用/检索命中的 spec_id（去重保序）
    elapsed_ms: int = 0
    error: dict | None = None
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.answer.strip())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SystemAnswer:
        return cls(
            item_id=str(d.get("item_id") or ""),
            question=str(d.get("question") or ""),
            system=str(d.get("system") or ""),
            answer=str(d.get("answer") or ""),
            contexts=list(d.get("contexts") or []),
            cited_specs=list(d.get("cited_specs") or []),
            elapsed_ms=int(d.get("elapsed_ms") or 0),
            error=d.get("error"),
            meta=dict(d.get("meta") or {}),
        )


def parse_b_cited_specs(retrieval_raw: str) -> list[str]:
    """从 Telco-RAG 检索整段里抽出引用的 spec_id（去重保序）。

    'document 3GPP 29272-i20.docx' → '29.272'。
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in _B_DOC_RE.finditer(retrieval_raw or ""):
        sid = normalize_spec_id(m.group(1))
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def b_raw_to_answer(raw: dict) -> SystemAnswer:
    """collect_b 写的精简 raw → SystemAnswer(B)：切 contexts + 抽 cited_specs。

    raw 形如 {item_id, question, answer, retrieval_raw, rephrased_query, elapsed_ms, error, model}。
    """
    retrieval_raw = str(raw.get("retrieval_raw") or "")
    answer = str(raw.get("answer") or "")
    error = raw.get("error")
    if error is None and not answer.strip():
        error = {"type": "EmptyAnswer", "detail": "Telco-RAG result 为空"}
    return SystemAnswer(
        item_id=str(raw.get("item_id") or ""),
        question=str(raw.get("question") or ""),
        system=SYSTEM_B,
        answer=answer,
        contexts=_split_contexts(retrieval_raw),
        cited_specs=parse_b_cited_specs(retrieval_raw),
        elapsed_ms=int(raw.get("elapsed_ms") or 0),
        error=error,
        meta={
            "model": raw.get("model"),
            "rephrased_query": raw.get("rephrased_query"),
        },
    )


def align_systems(records_by_system: dict[str, Iterable[dict]]) -> dict:
    """按 item_id 对齐 N 个系统的 SystemAnswer-dict → 统一 results 结构（item_id 并集，保序）。

    每个 item 形如 {item_id, question, <sys>: dict|None, ...}；缺失某系统记 None
    （便于报告标记"某系统漏题/采集失败"）。系统键顺序 = 入参 dict 顺序。
    """
    by_sys: dict[str, dict[str, dict]] = {
        sys: {str(r.get("item_id")): r for r in recs} for sys, recs in records_by_system.items()
    }
    ordered_ids = list(dict.fromkeys(iid for m in by_sys.values() for iid in m))
    items: list[dict] = []
    for iid in ordered_ids:
        present = [by_sys[s].get(iid) for s in by_sys]
        question = str(next((p for p in present if p), {}).get("question") or "")
        item: dict = {"item_id": iid, "question": question}
        for s in by_sys:
            item[s] = by_sys[s].get(iid)
        items.append(item)
    return {"n_items": len(items), "items": items}


def align(a_records: Iterable[dict], b_records: Iterable[dict]) -> dict:
    """两系统对齐（向后兼容包装；新代码用 align_systems）。"""
    return align_systems({SYSTEM_A: a_records, SYSTEM_B: b_records})


# === JSONL IO =============================================================


def dump_jsonl(records: Iterable[dict], path: Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_questions(path: Path) -> list[dict]:
    """读题集 JSONL：每行至少含 {item_id, question}。"""
    out: list[dict] = []
    for rec in load_jsonl(path):
        iid = str(rec.get("item_id") or "").strip()
        q = str(rec.get("question") or "").strip()
        if iid and q:
            out.append({"item_id": iid, "question": q})
    return out


__all__ = [
    "SYSTEM_A",
    "SYSTEM_B",
    "SystemAnswer",
    "align",
    "align_systems",
    "b_raw_to_answer",
    "dump_jsonl",
    "load_jsonl",
    "load_questions",
    "parse_b_cited_specs",
]
