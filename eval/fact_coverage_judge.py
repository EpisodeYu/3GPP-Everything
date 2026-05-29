"""Fact coverage LLM judge：判定 agent 答案是否覆盖了 expected_facts 中每条事实。

设计动机（详见 docs/04-handoff/2026-05-29-fact-coverage-llm-judge.md）：
M7.1 原 substring 口径（`f.lower() in answer.lower()`）有三类系统性 false-negative：
- paraphrase 失败：`使得 ... 的大小等于 ...` vs `使其大小与 ... 相等` → MISS
- 答案诚实拒答数值类 fact 时（"未在 chunks 中找到"）数字必然不出现 → MISS
- expected_fact 是长句时，连字符 / 标点 / 语序任一差异都漏

→ 2026-05-29 决议：彻底改 LLM judge，按"answer 是否覆盖该 fact 的事实内容"判，
   允许同义改写、数值等价、单位换算；substring 字段保留作诊断 + LLM 失败时的
   fallback（详见 runner.py::compute_eval_metrics）。

判定 schema（每条 expected_fact 独立打分，每题一次 LLM call 批量返回）：
- HIT：答案陈述了 expected_fact 的事实内容（同义改写 / 数值等价 / 顺序差异均算）
- PARTIAL：答案提到该 fact 但缺漏一半 / 数值不完全对得上 / 仅一侧
- MISS：答案完全没覆盖该 fact，或在该点上明确说"未在资料中找到"

聚合公式：score = (HIT*1 + PARTIAL*0.5) / total

接口约定：
- judge LLM：`mimo-v2.5-pro`（settings.llm_fact_coverage_judge_model）+ temperature=0.01
  + function_calling，沿用 negative_judge 已验证的通路（避开 deepseek-v4-pro reasoning
  mode 不能 tool_choice 的坑；详见 negative_judge.py 顶部注释）
- 单题异常隔离：任何异常 → score=None, verdicts=[], reason 写错误摘要；不挂 runner
- 仅对 `item.expected_facts` 非空 + answer 非空时调用；否则 skipped

usage（典型）：
    from eval.fact_coverage_judge import build_default_fact_coverage_judge
    judge = build_default_fact_coverage_judge()  # 缺 key → FactCoverageJudgeError
    out = judge.score_item(item, resp)
    # out = {"score": 0.83, "verdicts": [...], "skipped": False, "reason": None}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from eval.runner_retrieval import GoldenItem
from eval.settings import EvalSettings, get_settings

if TYPE_CHECKING:
    from eval.runner import AgentResponse

log = logging.getLogger(__name__)

FactVerdict = Literal["HIT", "PARTIAL", "MISS"]

ALLOWED_FACT_VERDICTS: frozenset[str] = frozenset({"HIT", "PARTIAL", "MISS"})

_VERDICT_WEIGHT: dict[str, float] = {"HIT": 1.0, "PARTIAL": 0.5, "MISS": 0.0}


class FactCoverageJudgeError(Exception):
    """fact_coverage_judge 对外异常基类（依赖缺失 / 配置错误）。"""


_PROMPT_ZH = """你是 3GPP RAG 评测裁判。逐条判定 agent 答案是否覆盖了"期望事实"列表中的每条事实。

问题：
{question}

Agent 答案：
{answer}

期望事实（请对每一条独立判定）：
{facts_block}

判定档次（每条事实**必须**从下面三档中选一个）：
- HIT：答案以事实层面陈述了该期望事实的内容。允许同义改写 / 重新组织 / 数值等价
  形式 / 单位换算 / 顺序差异。**不要求**字面一致；只要"在事实层面表达了同样的内容"
  即算 HIT。
- PARTIAL：答案提到该事实涉及的话题，但描述不完整 / 仅覆盖一半 / 措辞偏差到边界
  （例如期望事实是"截断 MSB 使两者大小相等"，答案只说"截断"但没提"使大小相等"）。
- MISS：答案完全没涵盖该事实，或在该点上明确说"未在资料中找到 / 规范未规定 /
  无法给出"。

强制规则（**严格遵守**）：
- **不要**因为答案提到了"相邻 / 相关概念"就给 HIT；事实必须被陈述出来。
- 数值类事实（数字 / 码率 / 频谱效率 / 调制阶数）**必须**在答案中出现该数值
  才算 HIT。等价形式 / 单位换算允许；但是"未给出具体数值" / 仅给范围且范围
  与期望不一致 → MISS。
- 拒答 / "未找到"类回答应当大量给 MISS（这是答案诚实但事实未覆盖；不要因为
  答案诚实就放水给 PARTIAL/HIT）。
- 当期望事实本身是同一概念的多种表述（中英两版、数值与单位）时，每条独立判定，
  不要因为另一条已 HIT 就连带改变这一条。

每条事实的 reason 字段：1 句中文，说明判断依据。"""

_PROMPT_EN = """You are a 3GPP RAG eval judge. For each "expected fact", decide whether the \
agent's answer covers that fact.

Question:
{question}

Agent answer:
{answer}

Expected facts (judge each one independently):
{facts_block}

Verdict rubric (each fact **must** receive exactly one of):
- HIT: the answer states the fact at a semantic level. Synonym paraphrase /
  re-ordering / equivalent numerical or unit-converted forms count. Verbatim
  match is NOT required.
- PARTIAL: the answer mentions the topic but the description is incomplete /
  only one side / borderline wording (e.g. expected fact says "truncate MSB to
  align sizes"; answer says "truncate" but does not mention size alignment).
- MISS: the answer does not cover the fact at all, OR explicitly says "not found
  in retrieval / not specified in the spec / cannot give the value" for this
  fact.

Hard rules (**strict**):
- Do NOT mark HIT just because the answer mentions an adjacent or related
  concept; the fact itself must be stated.
- For numerical facts (rate / spectral efficiency / modulation order), the same
  value MUST appear in the answer to score HIT. Equivalent forms / unit
  conversions are allowed; but "no specific value given" or a range that
  disagrees with the expected value → MISS.
- Refusal / "not found" answers should yield MOSTLY MISS (this is intentional;
  the answer is honest but the fact is not covered. Do not soften to PARTIAL
  just because the answer is honest).
- When several expected facts express the same concept in different forms
  (Chinese vs English, value vs unit), judge each one independently; do not
  let one HIT spill over to another.

Reason field: 1 short English sentence per fact, explaining the verdict."""


def _format_facts_block(facts: list[str]) -> str:
    """把 expected_facts 列表 → numbered Markdown list（prompt 内嵌）。"""
    return "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts))


@dataclass(slots=True)
class FactCoverageJudge:
    """单题异常隔离的 fact-coverage judge。

    一题一次 LLM call，返回 list[(fact, verdict, reason)] + 加权总分。
    """

    llm: Any  # langchain ChatModel with `.with_structured_output(schema, method=...)` 支持

    def score_item(
        self,
        item: GoldenItem,
        resp: AgentResponse,
    ) -> dict[str, Any]:
        """对单条 item 跑一次 LLM judge，返回：

            {
                "score": float | None,                # weighted = (HIT + 0.5*PARTIAL) / total
                "verdicts": list[dict] | None,        # [{fact, verdict, reason}, ...]
                "skipped": bool,                      # 空答案 / 空 expected_facts → True
                "reason": str | None,                 # skipped / judge_error 摘要
            }

        - 空答案 / 空 expected_facts：skipped=True, score=None；不打 LLM。
        - LLM / schema 异常：score=None, verdicts=None, reason 写错误摘要。
        """
        facts = [f.strip() for f in (item.expected_facts or []) if f and f.strip()]
        if not facts:
            return {
                "score": None,
                "verdicts": None,
                "skipped": True,
                "reason": "skipped: empty expected_facts",
            }
        if not resp.answer:
            return {
                "score": None,
                "verdicts": None,
                "skipped": True,
                "reason": "skipped: empty answer",
            }

        prompt = (_PROMPT_ZH if item.language == "zh" else _PROMPT_EN).format(
            question=item.question,
            answer=resp.answer,
            facts_block=_format_facts_block(facts),
        )

        try:
            raw = self._invoke_structured(prompt, n_facts=len(facts))
        except Exception as exc:
            log.warning("fact_coverage_judge crashed on %s: %s", item.id, exc)
            return {
                "score": None,
                "verdicts": None,
                "skipped": False,
                "reason": f"judge_error: {exc}"[:300],
            }

        verdicts_raw = raw.get("verdicts") or []
        if not isinstance(verdicts_raw, list):
            log.warning(
                "fact_coverage_judge returned non-list verdicts on %s: %r",
                item.id,
                type(verdicts_raw).__name__,
            )
            return {
                "score": None,
                "verdicts": None,
                "skipped": False,
                "reason": "judge_unknown_shape: verdicts not a list",
            }

        verdicts: list[dict[str, Any]] = []
        # LLM 偶发漏判 / 多判：按 expected_facts 顺序对齐，超出 / 缺失 → 该条 verdict=None
        # （视作 MISS，避免 LLM 漏返一条就让整体 score 偏高）
        by_index = list(verdicts_raw)
        weight_sum = 0.0
        valid_count = 0
        for idx, fact in enumerate(facts):
            entry = by_index[idx] if idx < len(by_index) else None
            verdict_val: str | None = None
            reason_val: str | None = None
            if isinstance(entry, dict):
                v = (entry.get("verdict") or "").strip().upper()
                if v in ALLOWED_FACT_VERDICTS:
                    verdict_val = v
                reason_raw = entry.get("reason")
                if isinstance(reason_raw, str) and reason_raw.strip():
                    reason_val = reason_raw.strip()
            verdicts.append(
                {
                    "fact": fact,
                    "verdict": verdict_val,
                    "reason": reason_val,
                }
            )
            if verdict_val is not None:
                weight_sum += _VERDICT_WEIGHT[verdict_val]
                valid_count += 1

        # 全条都未拿到合法 verdict → 视作 LLM 失效，分数留 None 让聚合跳过
        if valid_count == 0:
            return {
                "score": None,
                "verdicts": verdicts,
                "skipped": False,
                "reason": "judge_unknown_verdicts: no fact got a legal verdict",
            }

        # 部分条目缺判：按 len(facts) 做分母（缺的当 0 权），保持指标语义不被
        # LLM 漏判抬高
        score = weight_sum / len(facts)
        return {
            "score": score,
            "verdicts": verdicts,
            "skipped": False,
            "reason": None,
        }

    def _invoke_structured(self, prompt: str, *, n_facts: int) -> dict[str, Any]:
        """走 with_structured_output(method='function_calling') 拿结构化结果。

        function_calling 在 LiteLLM proxy → mimo-v2.5-pro 上验证可用（与 negative_judge
        同款；2026-05-23 起 deepseek-v4-pro reasoning mode 不支持 tool_choice，本路径
        故意不切到 deepseek，避免回归 GLM 时期的 1e-8 / 400 那批坑）。

        `n_facts` 仅用于 schema description 提示 LLM 期望返回长度；
        实际对齐在 score_item 内按 index 兜底。
        """
        from pydantic import BaseModel, Field

        class _FactVerdict(BaseModel):
            fact: str = Field(description="The expected fact verbatim from input list")
            verdict: str = Field(description="One of HIT / PARTIAL / MISS, all uppercase")
            reason: str = Field(description="One short sentence justifying the verdict")

        class _Schema(BaseModel):
            verdicts: list[_FactVerdict] = Field(
                description=(
                    f"Verdicts in the SAME order as the expected facts list, "
                    f"length = {n_facts}. Do not skip or reorder facts."
                )
            )

        chain = self.llm.with_structured_output(_Schema, method="function_calling")
        result = chain.invoke(prompt)
        if isinstance(result, _Schema):
            return {"verdicts": [v.model_dump() for v in result.verdicts]}
        if isinstance(result, dict):
            # 注意：verdicts 字段透传不强转。caller 用 isinstance(..., list) 检测
            # garbage shape（直接 list("not a list") 会把字符串拆字符，掩盖错误）
            return {"verdicts": result.get("verdicts")}
        raise FactCoverageJudgeError(f"unexpected structured output type: {type(result).__name__}")


def build_default_fact_coverage_judge(
    settings: EvalSettings | None = None,
) -> FactCoverageJudge:
    """按 06-md §4 默认：fact_coverage_judge 用 `llm_fact_coverage_judge_model`
    （默认 mimo-v2.5-pro）走 function_calling；与 negative_judge 同款 LLM 通路。

    与 ragas judge (`llm_judge_model`, 默认 deepseek-v4-pro) 错开，因为 deepseek-v4
    系列 reasoning mode 不支持 `tool_choice` → with_structured_output 会 400。"""
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise FactCoverageJudgeError("LITELLM_API_KEY missing; fact_coverage judge LLM 无法初始化")
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise FactCoverageJudgeError(
            "langchain-openai not installed. Run: uv sync --project eval --extra ragas"
        ) from exc

    llm = ChatOpenAI(
        model=s.llm_fact_coverage_judge_model,
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        temperature=0.01,
    )
    return FactCoverageJudge(llm=llm)


__all__ = [
    "ALLOWED_FACT_VERDICTS",
    "FactCoverageJudge",
    "FactCoverageJudgeError",
    "FactVerdict",
    "build_default_fact_coverage_judge",
]
