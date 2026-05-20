"""Ragas 接入（M7.2）：在 eval.runner 已生成的 (item, AgentResponse) 上算 4 metric。

接口约定（详见 docs/03-development/06-evaluation-and-observability.md §5）：
- judge LLM：`glm-5.1`（temperature=0.01）；与 Agent 用的 `mimo-v2.5-pro` 错开避免同源偏差。
  注：langchain_openai 把 `temperature=0` 编码成 `1e-08`，GLM provider 视为越界（开区间 (0,1)）
  返回 400；0.01 是它能接受的最低值，对 judge 的"近似贪心"特性影响可忽略
- 评估 embedding：`voyage-4-large`（与 RAG 索引同款，走 LiteLLM OpenAI 兼容端点）
- 4 metric：faithfulness / answer_relevancy / context_recall / context_precision
- 单题异常隔离：任一 metric 计算失败 → log warning + 该项填 None；不挂整个 runner

设计要点：
- ragas / langchain-openai 走 optional-deps `ragas`，未装时 import 这模块只在调用计算时才报错
- 单题模式（一题一次 evaluate）：换 batch 模式时单题失败会污染整批；单题模式同时满足"异常隔离"硬要求
- contexts 取 chunks_rerank → chunks_hit → citations 的 fallback（与 runner 一致）
- ground_truth：item.expected_facts 拼成一段；空 list 则用 expected_specs 的 sections 名做 fallback
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
_METRIC_NAME_MAP: dict[str, str] = {
    "faithfulness": "ragas_faithfulness",
    "answer_relevancy": "ragas_answer_relevance",
    "context_recall": "ragas_context_recall",
    "context_precision": "ragas_context_precision",
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
    """ragas context_recall 需要的 ground truth；用 expected_facts 拼接。

    空 list（如 negative 题）回退到 expected_specs 的 spec_id 拼接；
    再空则给一个占位串（让 ragas 不至于 crash）。
    """
    if item.expected_facts:
        return " ".join(item.expected_facts)
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
    ) -> dict[str, float | None]:
        """对单题计算 4 metric。

        返回 dict 必含 4 个 key（缺的 = None）。任一异常 → log warning + 整体退化。
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
            ev_result = evaluate(
                ds,
                metrics=list(self.metrics),
                llm=self.llm,
                embeddings=self.embeddings,
                raise_exceptions=False,
                show_progress=False,
            )
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
            scores[field_name] = _coerce_score(raw)
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
    """按 06-...md §5 默认：judge=glm-5.1 + embedding=voyage-4-large，都走 LiteLLM。

    缺 `LITELLM_API_KEY` → 抛 RagasError；缺 ragas / langchain-openai 包 → 同。
    """
    s = settings or get_settings()
    if not s.litellm_api_key:
        raise RagasError("LITELLM_API_KEY missing; ragas judge LLM 无法初始化")
    try:
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import (
            answer_relevancy,
            context_precision,
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

    return RagasScorer(
        llm=_GLMSafeLangchainLLMWrapper(judge),
        embeddings=LangchainEmbeddingsWrapper(embed),
        metrics=[faithfulness, answer_relevancy, context_recall, context_precision],
    )


__all__ = [
    "RAGAS_METRIC_FIELDS",
    "RagasError",
    "RagasScorer",
    "build_default_ragas_scorer",
]
