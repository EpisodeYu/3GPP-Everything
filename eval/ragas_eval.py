"""Ragas 接入（M7.2）：在 eval.runner 已生成的 (item, AgentResponse) 上算 4 metric。

接口约定（详见 docs/03-development/06-evaluation-and-observability.md §5）：
- judge LLM：`deepseek-v4-pro`（temperature=0.01）；与 Agent 用的 `mimo-v2.5-pro`
  错开避免同源偏差。2026-05-23 起从 `glm-5.1` 切到 `deepseek-v4-pro`（成本 ≈ -50%/-75%）。
  注：langchain_openai 把 `temperature=0` 编码成 `1e-08`，沿用 0.01 是为兼容 GLM 时
  期遗留的 provider 容差（DeepSeek 自身接受 0；保留 0.01 让 judge 输出保持"近似贪心"特性）
- 评估 embedding：`voyage-4-large`（与 RAG 索引同款，走 LiteLLM OpenAI 兼容端点）
- 4 metric：faithfulness / answer_relevancy / context_recall / context_precision
- 单题异常隔离：任一 metric 计算失败 → log warning + 该项填 None；不挂整个 runner

设计要点：
- ragas / langchain-openai 走 optional-deps `ragas`，未装时 import 这模块只在调用计算时才报错
- 单题模式（一题一次 evaluate）：换 batch 模式时单题失败会污染整批；单题模式同时满足"异常隔离"硬要求
- contexts 取 chunks_rerank → chunks_hit → citations 的 fallback（与 runner 一致）
- **context_precision 用 `ContextUtilization` 而非 `ContextPrecision`（2026-05-30 起切换）**：
  ContextPrecision 用 reference 判 chunk 是否 useful；当 reference 是多个 fact 拼成的多
  sentence 时，judge 倾向于挑剔（"chunk 只覆盖一个 fact 不算 useful"），导致 false negatives。
  ContextUtilization 用 agent answer 判，agent answer 是连贯文本，judge 判定更稳定。
  实测对比（v7 同 56 题）：with-reference 0.669 vs without-reference **0.889 (+22pp)**。
  语义上：utilization 衡量"召回的 chunk 是否真被 agent 用上"，与我们关心的"RAG 链路质量"
  对齐度更高（rerank 出来的 chunk 没被 agent 用 = retrieval signal 失效，正是想测的）。
- **answer_relevance 双层修正（2026-05-30 起）**：
  1. noncommittal 投票从 `np.any`（任意一票即作废）改 majority vote
     (`sum(noncommittal) > strictness/2`)；
  2. 即使 majority 判 noncommittal 也**不归零**，而是用 `cosine_sim × 0.5` 折扣
     —— 保留答案与问题的语义相关度信号。
  理由：本项目已有独立 `negative_judge` 处理"该不该拒答 + 拒答对不对"的语义；
  ragas answer_relevance 在我们体系下只应当衡量"答案与问题的语义相关度"，不必再
  充当 noncommittal 检测器。v6+ prompt 鼓励 "未在 chunks 中找到 X" 这类诚实拒答
  在原 metric 下被打硬 0；新设计在保留 noncommittal 信号的同时不丢失相关度证据。
- 详见 docs/04-handoff/2026-05-30-ragas-metric-swap-utilization.md.
- ground_truth：item.expected_facts 以 sentence 边界（". "）拼成一段；空 list 则用 expected_specs
  的 spec_id 拼接。**为什么用 ". " 不用 " "**：ragas `LLMContextRecall` 把 reference 当成 "答案"
  按 sentence 拆 statement，每条问 judge "是否在 retrieved context 里"；空格拼接的 token soup
  拆出来全是无主语短语，judge 一律判 No，造成假 0（2026-05-29 ablate 实验实测 13 个零中
  8 题用句号拼后 ctx_recall 0.0→0.80+，详见 04-handoff/2026-05-29-ragas-4metric-uplift-plan.md §4）
- evaluate(...) 默认会 ping `OPENAI_API_KEY`；本模块通过 LangchainLLMWrapper(ChatOpenAI(...))
  完全注入自定义 client，避免任何对真 OpenAI 的 fallback 调用

usage（典型）：
    from eval.ragas_eval import RagasScorer, build_default_ragas_scorer
    scorer = build_default_ragas_scorer()   # 缺 LITELLM_API_KEY → 抛 RagasError
    results = await run_eval(GOLDEN, client=..., auth_token=..., ragas_scorer=scorer)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from eval.runner_retrieval import GoldenItem
from eval.settings import EvalSettings, get_settings

if TYPE_CHECKING:
    from eval.runner import AgentResponse

log = logging.getLogger(__name__)

# 4 metric 字段名（与 EvalResult 字段对齐）
RAGAS_METRIC_FIELDS = (
    "ragas_faithfulness",
    "ragas_answer_relevance",
    "ragas_context_recall",
    "ragas_context_precision",
)

# ragas 0.2 metric → EvalResult 字段名映射
# 注：`ragas_context_precision` 字段同时接受 with-reference / without-reference 两种命名，
# 因为 2026-05-30 起我们用 `ContextUtilization`（name=context_utilization）替换原
# `ContextPrecision`（name=context_precision），但下游字段名保持稳定向后兼容。
_METRIC_NAME_MAP: dict[str, str] = {
    "faithfulness": "ragas_faithfulness",
    "answer_relevancy": "ragas_answer_relevance",
    "context_recall": "ragas_context_recall",
    "context_precision": "ragas_context_precision",
    "context_utilization": "ragas_context_precision",
    "llm_context_precision_without_reference": "ragas_context_precision",
}


class RagasError(Exception):
    """ragas 评估对外异常基类（依赖缺失 / 配置错误等）。"""


def _empty_metric_dict() -> dict[str, float | None]:
    return {f: None for f in RAGAS_METRIC_FIELDS}


def _extract_contexts(resp: AgentResponse) -> list[str]:
    """从 AgentResponse 拼 ragas 用的 contexts 列表（fallback 顺序与 runner 一致）。"""
    src = resp.chunks_rerank or resp.chunks_hit or resp.citations
    out: list[str] = []
    for c in src:
        text = c.get("content") or c.get("text") or c.get("snippet") or ""
        if isinstance(text, str) and text.strip():
            out.append(text)
            continue
        # 没原文时退化为 spec+section 字符串占位，避免空 contexts 让 ragas 计算无意义
        sid = str(c.get("spec_id") or "").strip()
        sec = c.get("section_path") or ""
        if isinstance(sec, list):
            sec = ".".join(str(x) for x in sec)
        if sid:
            placeholder = f"{sid} §{sec}".strip() if sec else sid
            out.append(placeholder)
    return out


def _ground_truth(item: GoldenItem) -> str:
    """ragas reference 字段（context_recall / context_precision 用）：
    expected_facts 用 sentence 边界 `". "` 拼接，每条 fact 视为一句独立断言。

    设计依据：ragas `LLMContextRecall._ascore` 把 reference 当 answer 喂给 judge，
    按 sentence 拆 statement 后逐条问 "is this attributable to context"。
    若直接 `" ".join(facts)` 拼成一长串无标点字符串，sentencizer 会把整个串
    当成一句长 statement —— judge 极易判 0；改用 `". "` 强制 sentence 边界，
    每条 fact 成独立判定，judge 才能逐条 attribute。

    空 list（如 negative 题）→ 回退到 expected_specs 的 spec_id 拼接；
    再空 → 占位串（让 ragas 不至于 crash）。

    2026-05-29 ablate 实验对照（hand-multi-007 / hand-table-008 实测）：
      ORIG `" ".join(...)`           : ctx_recall = 0.00
      SENT `". ".join(...) + "."`    : ctx_recall = 0.80 / 0.83   ← 当前选择
      WRAPPED `f"The answer ... {f}"`: ctx_recall = 0.80 / 0.67
      SECTION 章节内容做 reference   : ctx_recall = 0.18 / 0.25 （变差，statement 数爆炸）
    """
    facts = [str(f).strip() for f in (item.expected_facts or []) if str(f).strip()]
    if facts:
        return ". ".join(facts) + "."
    if item.expected_specs:
        return " ".join(s.spec_id for s in item.expected_specs)
    return "(no ground truth)"


class _RagasMetric(Protocol):
    """ragas Metric 的最小协议（避免 type-check 时强依赖 ragas）。"""

    name: str


@dataclass(slots=True)
class RagasScorer:
    """单题异常隔离的 ragas 打分器。

    持有 LLM + embeddings wrapper 与 4 个 metric 实例；`score_item` 对单题调
    `ragas.evaluate(dataset_with_1_row)`，任一 metric 抛异常或返回 NaN → 该项 None。

    线程安全：ragas 内部用全局 cache + asyncio.Runner；建议同一进程顺序调用
    （runner.run_eval 顺序执行，符合）。
    """

    llm: Any  # LangchainLLMWrapper
    embeddings: Any  # LangchainEmbeddingsWrapper
    metrics: list[_RagasMetric]

    def score_item(
        self,
        item: GoldenItem,
        resp: AgentResponse,
        *,
        run_config: Any = None,
    ) -> dict[str, float | None]:
        """对单题计算 4 metric。

        返回 dict 必含 4 个 key（缺的 = None）。任一异常 → log warning + 整体退化。

        `run_config`：可选 ragas `RunConfig`，控制 per-job timeout / 并发。默认 None
        用 ragas 内置默认（timeout=180s，长答案 faithfulness 易 TimeoutError 丢样本）；
        重试失败题时传更长 timeout + 串行（max_workers=1）以救回超时项。
        """
        scores = _empty_metric_dict()
        contexts = _extract_contexts(resp)
        if not contexts or not resp.answer:
            # ragas 在 empty contexts / empty answer 上行为不稳；直接 None 占位
            log.warning(
                "ragas skip item=%s: empty contexts or answer (contexts=%d, answer_len=%d)",
                item.id,
                len(contexts),
                len(resp.answer or ""),
            )
            return scores

        try:
            from datasets import Dataset
            from ragas import evaluate
        except ImportError as exc:
            raise RagasError(
                "ragas / datasets not installed. Run: uv sync --project eval --extra ragas"
            ) from exc

        row = {
            "question": item.question,
            "answer": resp.answer,
            "contexts": contexts,
            "ground_truth": _ground_truth(item),
            # ragas 0.2 单题列名兼容（不同 metric 取不同字段名，多塞一份兜底）
            "user_input": item.question,
            "response": resp.answer,
            "retrieved_contexts": contexts,
            "reference": _ground_truth(item),
        }
        try:
            ds = Dataset.from_list([row])
            eval_kwargs: dict[str, Any] = dict(
                metrics=list(self.metrics),
                llm=self.llm,
                embeddings=self.embeddings,
                raise_exceptions=False,
                show_progress=False,
            )
            if run_config is not None:
                eval_kwargs["run_config"] = run_config
            ev_result = evaluate(ds, **eval_kwargs)
        except Exception as exc:
            log.warning("ragas evaluate() failed for item=%s: %s", item.id, exc)
            return scores

        # ragas 0.2: EvaluationResult 是 dataframe-like；.scores 是 list[dict]
        # 也兼容 .to_pandas() / [metric_name] 取值
        try:
            row_scores = self._extract_scores(ev_result)
        except Exception as exc:
            log.warning("ragas score extract failed for item=%s: %s", item.id, exc)
            return scores

        for metric_name, field_name in _METRIC_NAME_MAP.items():
            raw = row_scores.get(metric_name)
            coerced = _coerce_score(raw)
            # 同字段多 metric key 时（如 ragas_context_precision 接受 context_precision
            # / context_utilization / llm_context_precision_without_reference 三种 ragas
            # 命名），用 "first non-None wins" 语义；避免后续 None 覆盖前面找到的有效值。
            if coerced is None and scores.get(field_name) is not None:
                continue
            scores[field_name] = coerced
        return scores

    @staticmethod
    def _extract_scores(ev_result: Any) -> dict[str, Any]:
        """把 ragas EvaluationResult 取第 0 行 → dict[metric_name → score]。"""
        # 优先取 .scores（ragas 0.2 EvaluationResult 暴露 list[dict]）
        scores_attr = getattr(ev_result, "scores", None)
        if scores_attr:
            if hasattr(scores_attr, "__getitem__"):
                first = scores_attr[0]
            else:
                first = next(iter(scores_attr))
            if isinstance(first, dict):
                return dict(first)
        # 退化：to_pandas().iloc[0].to_dict()
        if hasattr(ev_result, "to_pandas"):
            df = ev_result.to_pandas()
            row0 = df.iloc[0].to_dict()
            return {k: v for k, v in row0.items() if isinstance(k, str)}
        # 最后兜底：EvaluationResult 本身是 mapping-like
        if hasattr(ev_result, "items"):
            return dict(ev_result.items())
        return {}


def _coerce_score(raw: Any) -> float | None:
    """ragas 单 metric 值 → float | None。NaN / 非数值 → None。"""
    if raw is None:
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    # NaN check（pandas / ragas 失败时常返回 NaN）
    if f != f:
        return None
    return f


def build_default_ragas_scorer(settings: EvalSettings | None = None) -> RagasScorer:
    """按 06-...md §5 默认：judge=deepseek-v4-pro + embedding=voyage-4-large，都走 LiteLLM。

    缺 `LITELLM_API_KEY` → 抛 RagasError；缺 ragas / langchain-openai 包 → 同。
    """
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise RagasError("LITELLM_API_KEY missing; ragas judge LLM 无法初始化")
    try:
        import numpy as np
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import (
            AnswerRelevancy,
            ContextUtilization,
            context_recall,
            faithfulness,
        )
    except ImportError as exc:
        raise RagasError(
            "ragas / langchain-openai not installed. Run: uv sync --project eval --extra ragas"
        ) from exc

    judge = ChatOpenAI(
        model=s.llm_judge_model,
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
        temperature=0.01,
    )
    embed = OpenAIEmbeddings(
        model=s.voyage_embedding_model,
        base_url=s.resolved_litellm_base_url,
        api_key=s.litellm_api_key,
    )

    class _GLMSafeLangchainLLMWrapper(LangchainLLMWrapper):
        """ragas `get_temperature()` 硬编码 1e-8，会强制覆盖 ChatOpenAI.temperature。

        GLM provider 把 1e-8 当越界返回 400；这里把 n=1 时的下界提到 0.01。
        """

        def get_temperature(self, n: int) -> float:
            return 0.3 if n > 1 else 0.01

    class _MajorityVoteAnswerRelevancy(AnswerRelevancy):
        """ragas 默认 `noncommittal` 用 `np.any` —— 3 次生成里任意一次 noncommittal → score=0。

        我们 v6+ prompt 鼓励诚实拒答 "未在 chunks 中找到 X"；ragas hair-trigger 一次假阳性
        → 整题归零。

        本子类做两层修正：
        1. 把 noncommittal 投票从 `any` 改成 majority（>= strictness/2 + 1 才算）；
        2. 即使 majority 判 noncommittal，**仍保留 cosine_sim 作为下界**（×0.5 折扣），
           而不是直接归零。理由：我们已有独立 `negative_judge` 处理"该不该拒答 + 拒答
           对不对"的语义；answer_relevance 在 v6+ 体系下只应当衡量"答案与问题的语义
           相关度"，不必再充当 noncommittal 检测器。

        这两步把 v6+ 诚实拒答的 4 道 zero-trap 题从硬 0 救到 ~0.3-0.45（cosine_sim
        × 0.5）；真"驴唇不对马嘴"的答案仍因 cosine_sim 本身低而拿不到分。
        """

        NONCOMMITTAL_DISCOUNT = 0.5

        def _calculate_score(self, answers, row):  # type: ignore[override]
            gen_questions = [a.question for a in answers]
            non_count = sum(int(a.noncommittal) for a in answers)
            noncommittal_majority = non_count > (len(answers) / 2)
            if all(q == "" for q in gen_questions):
                return float("nan")
            cosine_sim = float(
                np.asarray(self.calculate_similarity(row["user_input"], gen_questions)).mean()
            )
            if noncommittal_majority:
                return cosine_sim * self.NONCOMMITTAL_DISCOUNT
            return cosine_sim

    # context_utilization = LLMContextPrecisionWithoutReference (基于 agent answer 而非 reference)
    # 详见模块 docstring 的 metric swap 说明
    return RagasScorer(
        llm=_GLMSafeLangchainLLMWrapper(judge),
        embeddings=LangchainEmbeddingsWrapper(embed),
        metrics=[
            faithfulness,
            _MajorityVoteAnswerRelevancy(),
            context_recall,
            ContextUtilization(),
        ],
    )


__all__ = [
    "RAGAS_METRIC_FIELDS",
    "RagasError",
    "RagasScorer",
    "build_default_ragas_scorer",
]
