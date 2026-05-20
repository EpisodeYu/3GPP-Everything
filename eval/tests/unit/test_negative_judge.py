"""单测 eval.negative_judge：三档枚举 + 异常隔离 + 词典回退。

这里**完全 mock LLM**；真 GLM 通路在集成测里验证（实跑 negative 16 题）。
"""

from __future__ import annotations

from typing import Any

import pytest

from eval.negative_judge import (
    ALLOWED_VERDICTS,
    NegativeJudge,
    NegativeJudgeError,
    build_default_negative_judge,
)
from eval.runner import AgentResponse
from eval.runner_retrieval import GoldenItem


def _neg_item(*, language: str = "zh") -> GoldenItem:
    return GoldenItem(
        id="hand-neg-001",
        category="negative",
        language=language,
        question="伪命题问题 X 是怎么定义的？",
        expected_specs=[],
        expected_facts=[],
        forbidden=["X"],
        must_say_not_found=True,
        source="hand_crafted",
    )


class _FakeStructured:
    """ChatOpenAI.with_structured_output(schema, method=...) 的最小桩。

    `.invoke(prompt)` 返回构造时给的 dict 或 raise 异常对象。
    """

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


class TestScoreItemHappyPath:
    @pytest.mark.parametrize(
        "verdict",
        ["VALID_REFUSAL", "PARTIAL_REFUSAL", "INVALID"],
    )
    def test_three_verdicts_returned_verbatim(self, verdict: str) -> None:
        llm = _FakeChat(output={"verdict": verdict, "reason": "因为 ..."})
        judge = NegativeJudge(llm=llm)
        result = judge.score_item(_neg_item(), AgentResponse(answer="不存在该字段"))
        assert result == {"verdict": verdict, "reason": "因为 ..."}

    def test_uses_function_calling_method(self) -> None:
        llm = _FakeChat(output={"verdict": "VALID_REFUSAL", "reason": "ok"})
        judge = NegativeJudge(llm=llm)
        judge.score_item(_neg_item(), AgentResponse(answer="x"))
        assert llm.calls and llm.calls[0]["method"] == "function_calling"

    def test_zh_question_uses_zh_prompt_path(self) -> None:
        """虽然 mock 不消费 prompt，但保证 zh/en 分支都执行不抛。"""
        for lang in ("zh", "en"):
            llm = _FakeChat(output={"verdict": "VALID_REFUSAL", "reason": "ok"})
            judge = NegativeJudge(llm=llm)
            r = judge.score_item(_neg_item(language=lang), AgentResponse(answer="x"))
            assert r["verdict"] == "VALID_REFUSAL"


class TestEmptyOrInvalid:
    def test_empty_answer_skipped(self) -> None:
        """answer 为空 → 不打 LLM，直接 verdict=None + reason 描述跳过。"""
        llm = _FakeChat(output={"verdict": "VALID_REFUSAL", "reason": "x"})
        judge = NegativeJudge(llm=llm)
        r = judge.score_item(_neg_item(), AgentResponse(answer=""))
        assert r["verdict"] is None
        assert "empty answer" in (r["reason"] or "")
        assert llm.calls == []  # 未调用

    def test_unknown_verdict_returns_none(self) -> None:
        llm = _FakeChat(output={"verdict": "MAYBE", "reason": "huh"})
        judge = NegativeJudge(llm=llm)
        r = judge.score_item(_neg_item(), AgentResponse(answer="x"))
        assert r["verdict"] is None
        assert "MAYBE" in (r["reason"] or "")

    def test_case_normalized(self) -> None:
        """大小写归一：valid_refusal → VALID_REFUSAL。"""
        llm = _FakeChat(output={"verdict": "valid_refusal", "reason": "ok"})
        judge = NegativeJudge(llm=llm)
        r = judge.score_item(_neg_item(), AgentResponse(answer="x"))
        assert r["verdict"] == "VALID_REFUSAL"


class TestExceptionIsolation:
    def test_llm_raises_returns_judge_error(self) -> None:
        llm = _FakeChat(raise_exc=RuntimeError("boom"))
        judge = NegativeJudge(llm=llm)
        r = judge.score_item(_neg_item(), AgentResponse(answer="x"))
        assert r["verdict"] is None
        assert "judge_error" in (r["reason"] or "")
        assert "boom" in (r["reason"] or "")

    def test_unexpected_output_type_raised_then_caught(self) -> None:
        # _invoke_structured 拿到非 BaseModel/dict 会 raise → score_item 兜底转 judge_error
        llm = _FakeChat(output="garbage-string")
        judge = NegativeJudge(llm=llm)
        r = judge.score_item(_neg_item(), AgentResponse(answer="x"))
        assert r["verdict"] is None
        assert "judge_error" in (r["reason"] or "")


class TestBuildDefault:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 临时清掉 settings 的 cache + key
        from eval.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
        monkeypatch.setenv("LITELLM_API_KEY", "")
        with pytest.raises(NegativeJudgeError, match="LITELLM_API_KEY"):
            build_default_negative_judge()
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_allowed_verdicts_constants() -> None:
    """守门：三档枚举不要漂移。"""
    assert {"VALID_REFUSAL", "PARTIAL_REFUSAL", "INVALID"} == ALLOWED_VERDICTS
