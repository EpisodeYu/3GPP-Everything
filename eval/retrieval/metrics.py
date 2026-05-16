"""Retrieval 评测指标（纯函数，无外部依赖）。

口径（docs/03-development/06-evaluation-and-observability.md §3.5 / §8）：

- spec_recall@k：top-k 命中 expected_specs 中至少一个 → 1
- section_recall@k：top-k 至少一个 hit 的 (spec_id, section_path) 匹配 expected
  - section 命中 = expected `sections` 列表中任一作为 hit.section_path 的前缀
  - "前缀"语义按章节路径段切分（"4.2" 可命中 "4.2.2.3"，但不可命中 "4.20"）
- precision@k：top-k 中 section 命中数 / k
- MRR：第一个 section 命中所在的 1 / rank（无命中 → 0）

负样本（expected_specs 为空 + must_say_not_found）：
- retrieval-only 评测无法判定 "答案说没找到"；负样本在 retrieval-only 跑里
  仅记录 top-k 是否仍返回任何 hit，不影响主指标
- 真正的负样本评判在 M4 Agent 端做（fact_coverage / forbidden_violations）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import mean


@dataclass(frozen=True, slots=True)
class ExpectedSpec:
    spec_id: str
    sections: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HitRef:
    """指标计算用的 hit 抽象（与 retrieval.Hit 解耦，方便单测构造）。"""

    spec_id: str
    section_path: tuple[str, ...] = ()
    clause: str = ""

    @classmethod
    def from_hit(cls, hit) -> HitRef:  # type: ignore[no-untyped-def]
        path = tuple(getattr(hit, "section_path", ()) or ())
        return cls(
            spec_id=str(getattr(hit, "spec_id", "")),
            section_path=tuple(str(x) for x in path),
            clause=str(getattr(hit, "clause", "") or ""),
        )


def _section_segments(s: str) -> tuple[str, ...]:
    """`"4.2.2.3"` → `("4", "2", "2", "3")`；空字符串 → `()`。"""
    if not s:
        return ()
    return tuple(p for p in s.strip().split(".") if p)


def is_section_prefix(expected: str, hit_section_path: Sequence[str]) -> bool:
    """expected（如 `"4.2"`）作为 hit 章节路径前缀是否成立。

    - hit 没 section → False
    - expected 段数 > hit 段数 → False
    - 段一一对应 → True

    例：
      - expected="4.2", hit=["4","2","2","3"] → True
      - expected="4.20", hit=["4","2"] → False（段不等）
      - expected="4.2", hit=["4","2"] → True
      - expected="4.2", hit=["4","20","1"] → False
    """
    exp_seg = _section_segments(expected)
    if not exp_seg:
        return False
    hit_seg: tuple[str, ...]
    if isinstance(hit_section_path, str):
        hit_seg = _section_segments(hit_section_path)
    else:
        hit_seg = tuple(str(x) for x in hit_section_path if str(x))
    if len(exp_seg) > len(hit_seg):
        return False
    # exp_seg 与切片 hit_seg[:len(exp_seg)] 长度已等长，strict=True 安全
    return all(a == b for a, b in zip(exp_seg, hit_seg[: len(exp_seg)], strict=True))


def is_section_hit(expected: ExpectedSpec, hit: HitRef) -> bool:
    """spec_id 必须相等；若 expected.sections 非空，则任一 section 作为前缀命中。"""
    if hit.spec_id != expected.spec_id:
        return False
    if not expected.sections:
        return True  # 整 spec 命中即算
    for sec in expected.sections:
        if is_section_prefix(sec, hit.section_path):
            return True
        # clause 字段也尝试一次（chunker 可能没填 section_path 仅填 clause）
        if hit.clause and is_section_prefix(sec, hit.clause.split(".")):
            return True
    return False


def is_spec_hit(expected_specs: Sequence[ExpectedSpec], hit: HitRef) -> bool:
    return any(hit.spec_id == e.spec_id for e in expected_specs)


def per_question_metrics(
    expected_specs: Sequence[ExpectedSpec],
    hits: Sequence[HitRef],
    *,
    k_list: Sequence[int] = (5, 10, 20),
) -> dict[str, float]:
    """单题 metrics。

    Returns dict with keys:
      - spec_recall@{k}    (0/1)
      - section_recall@{k} (0/1)
      - precision@{k}      (0..1)
      - mrr                (0..1，按 section 命中)
      - mrr_spec           (0..1，按 spec 命中)
      - hits_total         (= len(hits))
    """
    out: dict[str, float] = {"hits_total": float(len(hits))}

    # MRR — 找第一个命中的 rank（1-based）
    mrr_section, mrr_spec = 0.0, 0.0
    for i, h in enumerate(hits, start=1):
        if mrr_section == 0.0 and any(is_section_hit(e, h) for e in expected_specs):
            mrr_section = 1.0 / i
        if mrr_spec == 0.0 and is_spec_hit(expected_specs, h):
            mrr_spec = 1.0 / i
        if mrr_section > 0 and mrr_spec > 0:
            break
    out["mrr"] = mrr_section
    out["mrr_spec"] = mrr_spec

    # Recall@k / Precision@k
    for k in k_list:
        sub = list(hits[:k])
        if not sub:
            out[f"spec_recall@{k}"] = 0.0
            out[f"section_recall@{k}"] = 0.0
            out[f"precision@{k}"] = 0.0
            continue
        spec_hit = any(is_spec_hit(expected_specs, h) for h in sub)
        sec_hits = [h for h in sub if any(is_section_hit(e, h) for e in expected_specs)]
        out[f"spec_recall@{k}"] = 1.0 if spec_hit else 0.0
        out[f"section_recall@{k}"] = 1.0 if sec_hits else 0.0
        out[f"precision@{k}"] = len(sec_hits) / float(k)

    return out


@dataclass(slots=True)
class RetrievalMetrics:
    """聚合 metrics（按 dim / 整体）。"""

    n_questions: int = 0
    spec_recall_at: dict[int, float] = field(default_factory=dict)
    section_recall_at: dict[int, float] = field(default_factory=dict)
    precision_at: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    mrr_spec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "n_questions": self.n_questions,
            "spec_recall_at": {f"@{k}": round(v, 4) for k, v in self.spec_recall_at.items()},
            "section_recall_at": {f"@{k}": round(v, 4) for k, v in self.section_recall_at.items()},
            "precision_at": {f"@{k}": round(v, 4) for k, v in self.precision_at.items()},
            "mrr": round(self.mrr, 4),
            "mrr_spec": round(self.mrr_spec, 4),
        }


def compute_metrics(
    rows: Sequence[dict[str, float]],
    *,
    k_list: Sequence[int] = (5, 10, 20),
) -> RetrievalMetrics:
    """聚合 per-question metrics（mean）。

    rows 来自多次 `per_question_metrics`；空 rows 返回零值 metrics。
    """
    if not rows:
        return RetrievalMetrics(n_questions=0)
    return RetrievalMetrics(
        n_questions=len(rows),
        spec_recall_at={k: mean(r[f"spec_recall@{k}"] for r in rows) for k in k_list},
        section_recall_at={k: mean(r[f"section_recall@{k}"] for r in rows) for k in k_list},
        precision_at={k: mean(r[f"precision@{k}"] for r in rows) for k in k_list},
        mrr=mean(r["mrr"] for r in rows),
        mrr_spec=mean(r["mrr_spec"] for r in rows),
    )


__all__ = [
    "ExpectedSpec",
    "HitRef",
    "RetrievalMetrics",
    "compute_metrics",
    "is_section_hit",
    "is_section_prefix",
    "is_spec_hit",
    "per_question_metrics",
]
