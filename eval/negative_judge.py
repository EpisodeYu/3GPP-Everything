"""Negative-题 LLM-as-judge：判定 agent 是否正确拒答了一道伪命题。

设计动机（详见 docs/04-handoff/2026-05-20-daily-eval-findings.md §2 + §3）：
M7.1 原 substring 口径（`is_not_found_answer` + forbidden 命中）有两个系统性
false-negative：
- 短语词表跟不上 LLM 真实生成的拒答措辞（"前提不成立" / "无法支持" / ...）
- forbidden 是纯 substring，拒答必须复述用户假设的概念才能否认它，必然误判

→ 2026-05-20 决议：彻底放弃 substring，用 LLM judge 对 negative 题做三档枚举。

判定 schema：
- VALID_REFUSAL：清楚指出伪前提不成立、概念不存在、规范未规定
- PARTIAL_REFUSAL：表面否认但中途编造了不存在的细节
- INVALID：完全没拒答，把伪前提当真在解释

接口约定：
- judge LLM：默认 `deepseek-v4-pro` + temperature=0.01 + function_calling（与 ragas judge
  同一管线，避免 ragas 1e-8 那个坑；详见 06-md §12 M7.2 "2026-05-20 GLM 温度修法"；
  2026-05-23 从 glm-5.1 切到 deepseek-v4-pro，成本降 ~50%/75%）
- 单题异常隔离：任何异常 → verdict=None, reason 含错误摘要；不挂 runner
- 仅对 `item.must_say_not_found and answer` 调用；其余 item 不打扰 LLM

usage（典型）：
    from eval.negative_judge import build_default_negative_judge
    judge = build_default_negative_judge()  # 缺 key → NegativeJudgeError
    results = await run_eval(GOLDEN, ..., negative_judge=judge)
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

Verdict = Literal["VALID_REFUSAL", "PARTIAL_REFUSAL", "INVALID"]

ALLOWED_VERDICTS: frozenset[str] = frozenset({"VALID_REFUSAL", "PARTIAL_REFUSAL", "INVALID"})


class NegativeJudgeError(Exception):
    """negative_judge 对外异常基类（依赖缺失 / 配置错误）。"""


_PROMPT_ZH = """你是 3GPP RAG 评测裁判。判定 agent 是否正确地拒答了一道"伪命题"问题。

伪命题问题（已知 3GPP 规范里不存在的概念 / 机制 / 字段）：
{question}

Agent 答案：
{answer}

判定标准（**仅三档，必须选一个**）：
- VALID_REFUSAL：答案清楚指出伪前提不成立 / 概念不存在 / 规范未规定 / 检索结果未支持；
  没有把伪概念当作真实存在的事实陈述。允许答案中提到伪概念名称用以否认它。
- PARTIAL_REFUSAL：答案表面上否认，但中途又编造了关于伪概念的"细节"、"机制"或
  "字段定义"（典型部分幻觉）。
- INVALID：答案完全没拒答，反而把伪前提当真在解释流程 / 给出数值 / 描述行为。

在 reason 字段里用 1-2 句中文说明判断依据。"""

_PROMPT_EN = """You are a 3GPP RAG eval judge. Decide whether the agent correctly \
refused to answer a "false premise" question.

Question (asserted to be a non-existent concept/mechanism/field in 3GPP specs):
{question}

Agent answer:
{answer}

Verdict rubric (**pick exactly one**):
- VALID_REFUSAL: answer clearly denies the false premise / says the concept does not
  exist / not specified in the spec / not supported by retrieval; does not assert the
  fake concept as a real fact. Quoting the fake concept name to deny it is allowed.
- PARTIAL_REFUSAL: answer superficially denies but then fabricates "details",
  "mechanisms", or "field definitions" about the fake concept (typical partial
  hallucination).
- INVALID: answer does not refuse at all; treats the false premise as true and
  explains procedure / gives values / describes behavior.

In `reason`, give 1-2 short English sentences explaining your judgment."""


@dataclass(slots=True)
class NegativeJudge:
    """单题异常隔离的 negative-judge。"""

    llm: Any  # langchain ChatModel with `.with_structured_output(schema, method=...)` 支持

    def score_item(
        self,
        item: GoldenItem,
        resp: AgentResponse,
    ) -> dict[str, str | None]:
        """对单条 negative item 跑一次 LLM judge。

        返回 dict 必含 `verdict`（VALID_REFUSAL / PARTIAL_REFUSAL / INVALID / None）+
        `reason`（裁判简述 / 异常摘要）。任何异常 → verdict=None + reason 写错误。
        """
        if not resp.answer:
            return {"verdict": None, "reason": "judge_skipped: empty answer"}

        prompt = (_PROMPT_ZH if item.language == "zh" else _PROMPT_EN).format(
            question=item.question, answer=resp.answer
        )
        try:
            raw = self._invoke_structured(prompt)
        except Exception as exc:
            log.warning("negative_judge crashed on %s: %s", item.id, exc)
            return {"verdict": None, "reason": f"judge_error: {exc}"[:300]}

        verdict_raw = (raw.get("verdict") or "").strip().upper()
        if verdict_raw not in ALLOWED_VERDICTS:
            log.warning("negative_judge returned unknown verdict on %s: %r", item.id, verdict_raw)
            return {
                "verdict": None,
                "reason": f"judge_unknown_verdict: {verdict_raw!r}",
            }
        reason = (raw.get("reason") or "").strip()
        return {"verdict": verdict_raw, "reason": reason or None}

    def _invoke_structured(self, prompt: str) -> dict[str, str]:
        """走 with_structured_output(method='function_calling') 拿结构化结果。

        function_calling 在 LiteLLM proxy → GLM 上验证可用（07:00 smoke），
        与 ragas 走 ChatOpenAI 默认路径触发 1e-8 那个坑无关。
        """
        from pydantic import BaseModel, Field

        class _Schema(BaseModel):
            verdict: str = Field(description="One of VALID_REFUSAL / PARTIAL_REFUSAL / INVALID")
            reason: str = Field(description="Short 1-2 sentence justification")

        chain = self.llm.with_structured_output(_Schema, method="function_calling")
        result = chain.invoke(prompt)
        if isinstance(result, _Schema):
            return {"verdict": result.verdict, "reason": result.reason}
        # 极个别 langchain 版本会返回 dict
        if isinstance(result, dict):
            return {
                "verdict": str(result.get("verdict") or ""),
                "reason": str(result.get("reason") or ""),
            }
        raise NegativeJudgeError(f"unexpected structured output type: {type(result).__name__}")


def build_default_negative_judge(settings: EvalSettings | None = None) -> NegativeJudge:
    """按 06-md §4.1 默认：negative_judge 用 `llm_negative_judge_model`（默认
    mimo-v2.5-pro）走 function_calling；与 ragas judge (`llm_judge_model`,
    默认 deepseek-v4-pro) 错开，因为 deepseek-v4 系列 reasoning mode 不支持
    `tool_choice` → with_structured_output 会 400。"""
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise NegativeJudgeError("LITELLM_API_KEY missing; negative judge LLM 无法初始化")
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise NegativeJudgeError(
            "langchain-openai not installed. Run: uv sync --project eval --extra ragas"
        ) from exc

    llm = ChatOpenAI(
        model=s.llm_negative_judge_model,
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        temperature=0.01,
    )
    return NegativeJudge(llm=llm)


__all__ = [
    "ALLOWED_VERDICTS",
    "NegativeJudge",
    "NegativeJudgeError",
    "Verdict",
    "build_default_negative_judge",
]
