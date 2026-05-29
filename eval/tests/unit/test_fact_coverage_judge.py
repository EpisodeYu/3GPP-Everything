"""单测 eval.fact_coverage_judge：三档加权 + 异常隔离 + 字段对齐。

完全 mock LLM；真 LiteLLM 通路在集成测里跑（v1 daily harness）。
"""

from __future__ import annotations

from typing import Any

import pytest

from eval.fact_coverage_judge import (
    ALLOWED_FACT_VERDICTS,
    FactCoverageJudge,
    FactCoverageJudgeError,
    build_default_fact_coverage_judge,
)
from eval.runner import AgentResponse
from eval.runner_retrieval import GoldenItem


def _item(
    *,
    facts: list[str] | None = None,
    language: str = "zh",
) -> GoldenItem:
    return GoldenItem(
        id="hand-fc-001",
        category="definition",
        language=language,
        question="测试问题：QPSK 等调制方案有哪些？",
        expected_specs=[],
        expected_facts=facts if facts is not None else ["QPSK", "16QAM", "256QAM"],
        forbidden=[],
        must_say_not_found=False,
        source="hand_crafted",
    )


class _FakeStructured:
    """ChatOpenAI.with_structured_output(...).invoke(prompt) 的最小桩。"""

    def __init__(self, *, output: Any = None, raise_exc: Exception | None = None) -> None:
        self._output = output
        self._raise = raise_exc

    def invoke(self, prompt: str) -> Any:
        if self._raise is not None:
            raise self._raise
        return self._output


class _FakeChat:
    """ChatOpenAI 的最小桩；记录 with_structured_output 的调用参数。"""

    def __init__(self, *, output: Any = None, raise_exc: Exception | None = None) -> None:
        self._output = output
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def with_structured_output(self, schema: Any, *, method: str) -> _FakeStructured:
        self.calls.append({"schema": schema, "method": method})
        return _FakeStructured(output=self._output, raise_exc=self._raise)


# === Happy path ===========================================================


class TestScoreItemHappyPath:
    def test_all_hit_score_one(self) -> None:
        llm = _FakeChat(
            output={
                "verdicts": [
                    {"fact": "QPSK", "verdict": "HIT", "reason": "提到"},
                    {"fact": "16QAM", "verdict": "HIT", "reason": "提到"},
                    {"fact": "256QAM", "verdict": "HIT", "reason": "提到"},
                ]
            }
        )
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(
            _item(),
            AgentResponse(answer="支持 QPSK / 16QAM / 256QAM 等多种调制"),
        )
        assert out["score"] == pytest.approx(1.0)
        assert out["skipped"] is False
        assert out["reason"] is None
        assert len(out["verdicts"]) == 3
        assert all(v["verdict"] == "HIT" for v in out["verdicts"])

    def test_all_miss_score_zero(self) -> None:
        llm = _FakeChat(
            output={
                "verdicts": [
                    {"fact": "QPSK", "verdict": "MISS", "reason": "答案未提及"},
                    {"fact": "16QAM", "verdict": "MISS", "reason": "答案未提及"},
                    {"fact": "256QAM", "verdict": "MISS", "reason": "答案未提及"},
                ]
            }
        )
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="未在 chunks 中找到"))
        assert out["score"] == pytest.approx(0.0)

    def test_partial_weighted_05(self) -> None:
        # 1 HIT + 1 PARTIAL + 1 MISS → (1 + 0.5 + 0) / 3 = 0.5
        llm = _FakeChat(
            output={
                "verdicts": [
                    {"fact": "QPSK", "verdict": "HIT", "reason": "在"},
                    {"fact": "16QAM", "verdict": "PARTIAL", "reason": "提了相关概念"},
                    {"fact": "256QAM", "verdict": "MISS", "reason": "缺"},
                ]
            }
        )
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="支持 QPSK"))
        assert out["score"] == pytest.approx(0.5)

    def test_uses_function_calling_method(self) -> None:
        llm = _FakeChat(output={"verdicts": [{"fact": "QPSK", "verdict": "HIT", "reason": "在"}]})
        judge = FactCoverageJudge(llm=llm)
        judge.score_item(_item(facts=["QPSK"]), AgentResponse(answer="支持 QPSK"))
        assert llm.calls and llm.calls[0]["method"] == "function_calling"

    def test_zh_and_en_prompts_both_runnable(self) -> None:
        for lang in ("zh", "en"):
            llm = _FakeChat(
                output={
                    "verdicts": [
                        {"fact": "QPSK", "verdict": "HIT", "reason": "ok"},
                    ]
                }
            )
            judge = FactCoverageJudge(llm=llm)
            out = judge.score_item(
                _item(facts=["QPSK"], language=lang),
                AgentResponse(answer="QPSK"),
            )
            assert out["score"] == pytest.approx(1.0)

    def test_lowercase_verdict_normalized(self) -> None:
        """LLM 偶尔小写返回 → 大写归一后仍命中。"""
        llm = _FakeChat(
            output={
                "verdicts": [
                    {"fact": "QPSK", "verdict": "hit", "reason": "在"},
                ]
            }
        )
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(facts=["QPSK"]), AgentResponse(answer="QPSK"))
        assert out["score"] == pytest.approx(1.0)
        assert out["verdicts"][0]["verdict"] == "HIT"


# === Skip / 边界 ==========================================================


class TestSkipped:
    def test_empty_answer_skipped(self) -> None:
        llm = _FakeChat(output={"verdicts": []})
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer=""))
        assert out["skipped"] is True
        assert out["score"] is None
        assert out["verdicts"] is None
        assert "empty answer" in (out["reason"] or "")
        assert llm.calls == []  # 未调用

    def test_empty_expected_facts_skipped(self) -> None:
        llm = _FakeChat(output={"verdicts": []})
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(facts=[]), AgentResponse(answer="something"))
        assert out["skipped"] is True
        assert out["score"] is None
        assert "empty expected_facts" in (out["reason"] or "")
        assert llm.calls == []

    def test_whitespace_facts_treated_empty(self) -> None:
        """空白事实条目会被剔除；全空白 → 视作空 expected_facts。"""
        llm = _FakeChat(output={"verdicts": []})
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(facts=["", "   "]), AgentResponse(answer="x"))
        assert out["skipped"] is True
        assert llm.calls == []


# === LLM 漏判 / 多判 / 未知档 / 异常 ======================================


class TestVerdictAlignment:
    def test_missing_one_verdict_counted_as_unjudged(self) -> None:
        """LLM 漏返一条：缺位的 fact verdict=None，分数按 len(facts) 做分母。"""
        # facts=[QPSK, 16QAM, 256QAM]；LLM 只返回 2 条 → 第三条对齐为 None
        llm = _FakeChat(
            output={
                "verdicts": [
                    {"fact": "QPSK", "verdict": "HIT", "reason": "在"},
                    {"fact": "16QAM", "verdict": "HIT", "reason": "在"},
                ]
            }
        )
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="QPSK 16QAM"))
        # (HIT + HIT + None_当0) / 3 = 2/3
        assert out["score"] == pytest.approx(2 / 3)
        assert out["verdicts"][2]["verdict"] is None

    def test_extra_verdicts_truncated(self) -> None:
        """LLM 多返回的条目应被忽略，按 len(facts) 截断。"""
        llm = _FakeChat(
            output={
                "verdicts": [
                    {"fact": "QPSK", "verdict": "HIT", "reason": "在"},
                    {"fact": "16QAM", "verdict": "HIT", "reason": "在"},
                    {"fact": "256QAM", "verdict": "HIT", "reason": "在"},
                    {"fact": "ghost", "verdict": "HIT", "reason": "幻觉"},
                ]
            }
        )
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="all"))
        assert out["score"] == pytest.approx(1.0)
        assert len(out["verdicts"]) == 3

    def test_unknown_verdict_treated_as_unjudged(self) -> None:
        """单条 verdict 不合法（"MAYBE"） → 该条 verdict=None，但其它仍计。"""
        llm = _FakeChat(
            output={
                "verdicts": [
                    {"fact": "QPSK", "verdict": "MAYBE", "reason": "?"},
                    {"fact": "16QAM", "verdict": "HIT", "reason": "在"},
                    {"fact": "256QAM", "verdict": "HIT", "reason": "在"},
                ]
            }
        )
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="x"))
        # (None_当0 + HIT + HIT) / 3 = 2/3
        assert out["score"] == pytest.approx(2 / 3)
        assert out["verdicts"][0]["verdict"] is None

    def test_all_unknown_returns_none_score(self) -> None:
        """全部 verdict 都不合法 → score=None（防止假高分）。"""
        llm = _FakeChat(
            output={
                "verdicts": [
                    {"fact": "QPSK", "verdict": "junk", "reason": "x"},
                    {"fact": "16QAM", "verdict": "?", "reason": "x"},
                    {"fact": "256QAM", "verdict": "MAYBE", "reason": "x"},
                ]
            }
        )
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="x"))
        assert out["score"] is None
        assert "no fact got a legal verdict" in (out["reason"] or "")

    def test_non_list_verdicts_returns_none(self) -> None:
        llm = _FakeChat(output={"verdicts": "not a list"})
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="x"))
        assert out["score"] is None
        assert out["verdicts"] is None
        assert "judge_unknown_shape" in (out["reason"] or "")


class TestExceptionIsolation:
    def test_llm_raises_returns_judge_error(self) -> None:
        llm = _FakeChat(raise_exc=RuntimeError("boom"))
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="x"))
        assert out["score"] is None
        assert out["verdicts"] is None
        assert out["skipped"] is False
        assert "judge_error" in (out["reason"] or "")
        assert "boom" in (out["reason"] or "")

    def test_unexpected_output_type_caught(self) -> None:
        # 非 dict / 非 BaseModel → _invoke_structured 抛 → score_item 兜底
        llm = _FakeChat(output="garbage-string")
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(), AgentResponse(answer="x"))
        assert out["score"] is None
        assert "judge_error" in (out["reason"] or "")


# === build_default ========================================================


class TestBuildDefault:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from eval.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
        monkeypatch.setenv("LITELLM_API_KEY", "")
        with pytest.raises(FactCoverageJudgeError, match="LITELLM_API_KEY"):
            build_default_fact_coverage_judge()
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_allowed_verdicts_constants() -> None:
    """守门：三档枚举不要漂移。"""
    assert {"HIT", "PARTIAL", "MISS"} == ALLOWED_FACT_VERDICTS
