"""成对盲评裁判：两系统答案匿名（甲/乙）对比 + 位置对冲（README §8.2）。

设计：
- **匿名**：裁判只看"甲/乙"两份答案，不知谁是哪个系统、看不到 contexts（否则暴露谁是 RAG）。
- **参考引导**：给裁判金标准 expected_facts 作评分准绳（正题比"覆盖事实"，负题比"是否正确拒答"）。
- **位置对冲**：同一对系统跑正反两序（甲/乙 互换），`aggregate_pair` 聚合，消位置偏好。
- 裁判 LLM = `glm-5.1`（与 A=mimo / B=gpt-4o-mini / C=deepseek 都不同源），走 function_calling
  （沿用 negative_judge 已验证的通路）。
- 单题异常隔离：任何异常 → verdict=None + reason 写错误摘要；不挂上层。

接口：
    judge = build_pairwise_judge()
    r = judge.score_pair(item, ans_x, ans_y)   # {'verdict': 'X'|'Y'|'TIE'|None, 'reason': ...}
    winner = aggregate_pair(v_ab, v_ba)         # '1' | '2' | 'TIE'（位置对冲后）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from eval.settings import EvalSettings, get_settings

if TYPE_CHECKING:
    from eval.runner_retrieval import GoldenItem

log = logging.getLogger(__name__)

DEFAULT_PAIRWISE_JUDGE_MODEL = "glm-5.1"

Verdict = Literal["X", "Y", "TIE"]
ALLOWED_VERDICTS: frozenset[str] = frozenset({"X", "Y", "TIE"})


class PairwiseJudgeError(Exception):
    """pairwise_judge 对外异常基类。"""


_PROMPT = """You are an impartial judge comparing two answers (甲 and 乙) to a 3GPP \
telecom question. You do not know which system produced which answer.

Question:
{question}

{rubric}

Answer 甲:
{ans_x}

Answer 乙:
{ans_y}

Decide which answer is better overall. Output verdict EXACTLY one of:
- "X"  — 甲 is better
- "Y"  — 乙 is better
- "TIE" — equally good / equally bad
Give a 1-2 sentence reason."""

_RUBRIC_POSITIVE = """Judge mainly by factual correctness against these reference facts \
the correct answer should convey (then completeness, grounding/citing the right spec, \
clarity). An answer that states wrong facts or cites the wrong spec is worse.
Reference facts:
{facts}"""

_RUBRIC_NEGATIVE = """This is a FALSE-PREMISE / OUT-OF-SCOPE question: the concept does \
not exist in 3GPP (or is not specified by 3GPP). The BETTER answer correctly refuses / \
says it is not specified / not found, WITHOUT fabricating details. An answer that \
plays along and invents specifics is worse."""


@dataclass(slots=True)
class PairwiseJudge:
    """单对异常隔离的成对裁判。"""

    llm: Any  # langchain ChatModel，支持 with_structured_output(method='function_calling')

    def score_pair(self, item: GoldenItem, ans_x: str, ans_y: str) -> dict[str, str | None]:
        """对 (甲=ans_x, 乙=ans_y) 跑一次裁判。返回 {'verdict': X/Y/TIE/None, 'reason': ...}。"""
        x, y = (ans_x or "").strip(), (ans_y or "").strip()
        if not x and not y:
            return {"verdict": "TIE", "reason": "both empty"}
        if not x:
            return {"verdict": "Y", "reason": "甲 empty"}
        if not y:
            return {"verdict": "X", "reason": "乙 empty"}

        if item.must_say_not_found:
            rubric = _RUBRIC_NEGATIVE
        else:
            facts = "\n".join(f"- {f}" for f in (item.expected_facts or [])) or "(none given)"
            rubric = _RUBRIC_POSITIVE.format(facts=facts)
        prompt = _PROMPT.format(question=item.question, rubric=rubric, ans_x=ans_x, ans_y=ans_y)

        try:
            raw = self._invoke_structured(prompt)
        except Exception as exc:
            log.warning("pairwise_judge crashed on %s: %s", item.id, exc)
            return {"verdict": None, "reason": f"judge_error: {exc}"[:300]}

        verdict = (raw.get("verdict") or "").strip().upper()
        if verdict not in ALLOWED_VERDICTS:
            log.warning("pairwise_judge unknown verdict on %s: %r", item.id, verdict)
            return {"verdict": None, "reason": f"judge_unknown_verdict: {verdict!r}"}
        return {"verdict": verdict, "reason": (raw.get("reason") or "").strip() or None}

    def _invoke_structured(self, prompt: str) -> dict[str, str]:
        from pydantic import BaseModel, Field

        class _Schema(BaseModel):
            verdict: str = Field(description="One of X (甲 better) / Y (乙 better) / TIE")
            reason: str = Field(description="1-2 sentence justification")

        chain = self.llm.with_structured_output(_Schema, method="function_calling")
        result = chain.invoke(prompt)
        if isinstance(result, _Schema):
            return {"verdict": result.verdict, "reason": result.reason}
        if isinstance(result, dict):
            return {
                "verdict": str(result.get("verdict") or ""),
                "reason": str(result.get("reason") or ""),
            }
        raise PairwiseJudgeError(f"unexpected structured output type: {type(result).__name__}")


_SCORE_X = {"X": 1.0, "Y": 0.0, "TIE": 0.5}


def aggregate_pair(v_ab: str | None, v_ba: str | None) -> str:
    """位置对冲聚合两序裁决 → 谁赢（'1' | '2' | 'TIE'）。

    - v_ab：系统1 当甲(X)、系统2 当乙(Y) 时的裁决
    - v_ba：系统2 当甲(X)、系统1 当乙(Y) 时的裁决（互换位置）
    每序给胜者 +1（TIE 各 +0.5），两序累加比大小。任一序裁判失败(None)→ 该序按 TIE 计。
    """
    a, b = v_ab if v_ab in ALLOWED_VERDICTS else "TIE", v_ba if v_ba in ALLOWED_VERDICTS else "TIE"
    s1 = _SCORE_X[a] + (1.0 - _SCORE_X[b])  # 序2里系统1是乙(Y)，得分 = 1 - X分
    s2 = (1.0 - _SCORE_X[a]) + _SCORE_X[b]
    if s1 > s2:
        return "1"
    if s2 > s1:
        return "2"
    return "TIE"


def build_pairwise_judge(
    settings: EvalSettings | None = None, model: str = DEFAULT_PAIRWISE_JUDGE_MODEL
) -> PairwiseJudge:
    """glm-5.1 成对裁判（与三方系统都不同源）。"""
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise PairwiseJudgeError("LITELLM_API_KEY missing; pairwise judge LLM 无法初始化")
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise PairwiseJudgeError(
            "langchain-openai not installed. Run: uv sync --project eval --extra ragas"
        ) from exc
    llm = ChatOpenAI(
        model=model,
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        temperature=0.01,
    )
    return PairwiseJudge(llm=llm)


__all__ = [
    "ALLOWED_VERDICTS",
    "DEFAULT_PAIRWISE_JUDGE_MODEL",
    "PairwiseJudge",
    "PairwiseJudgeError",
    "Verdict",
    "aggregate_pair",
    "build_pairwise_judge",
]
