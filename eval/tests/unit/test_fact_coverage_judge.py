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


def _verdicts_to_ai_msg(output: Any) -> Any:
    """把 `{"verdicts": [...]}` 包成最简 AIMessage with tool_calls。"""

    class _Msg:
        def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
            self.tool_calls = tool_calls

    if output is None:
        return _Msg([])
    if isinstance(output, dict) and "verdicts" in output:
        return _Msg([{"name": "_Schema", "args": output, "id": "c-1", "type": "tool_call"}])
    # 其它（比如直接给 string / 别的 shape） — 让 args 字段走 dict 校验路径
    return _Msg([{"name": "_Schema", "args": output, "id": "c-1", "type": "tool_call"}])


class _FakeBoundInvoker:
    """`llm.bind_tools(...).invoke(prompt)` 返回的 chain 桩。"""

    def __init__(self, *, ai_message: Any = None, raise_exc: Exception | None = None) -> None:
        self._ai = ai_message
        self._raise = raise_exc

    def invoke(self, prompt: str) -> Any:
        if self._raise is not None:
            raise self._raise
        return self._ai


class _FakeChat:
    """ChatOpenAI 的最小桩（bind_tools 路径）；保留 `calls` API 与旧测试兼容。

    构造参数 `output` 形如 `{"verdicts": [...]}`，会被自动包成
    AIMessage.tool_calls[0].args；也允许直接传 AIMessage-shape 对象。
    """

    def __init__(self, *, output: Any = None, raise_exc: Exception | None = None) -> None:
        # 旧测试通过 `output={"verdicts": [...]}` 期望被 invoke 返回；
        # bind_tools 路径下我们包成 tool_call 形式
        if output is not None and not hasattr(output, "tool_calls"):
            self._ai = _verdicts_to_ai_msg(output)
        else:
            self._ai = output
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def bind_tools(
        self,
        tools: list[Any],
        *,
        tool_choice: Any | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> _FakeBoundInvoker:
        self.calls.append(
            {
                "tools": tools,
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
            }
        )
        return _FakeBoundInvoker(ai_message=self._ai, raise_exc=self._raise)


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

    def test_bind_tools_with_forced_tool_choice(self) -> None:
        """生产 path 改走 bind_tools + tool_choice 强制命中 _Schema。"""
        llm = _FakeChat(output={"verdicts": [{"fact": "QPSK", "verdict": "HIT", "reason": "在"}]})
        judge = FactCoverageJudge(llm=llm)
        judge.score_item(_item(facts=["QPSK"]), AgentResponse(answer="支持 QPSK"))
        assert llm.calls
        choice = llm.calls[0]["tool_choice"]
        assert choice and choice["function"]["name"] == "_Schema"
        assert llm.calls[0]["parallel_tool_calls"] is False

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


# === pydantic before-validator（2026-05-29 实测 mimo-v2.5-pro 复现）==========


class TestPreParseVerdictsValidator:
    """mimo-v2.5-pro function_calling 偶发把 verdicts 编码成 JSON 字符串。

    `_pre_parse_verdicts` before-validator 应在 pydantic 实例化前 json.loads
    兜底。覆盖：
    - 字符串形式 list → 自动解析成功
    - 字符串非合法 JSON → 透传，让原 ValidationError 抛出
    - 字符串非 list shape（json.loads 解出 dict / 数字） → 透传，让原
      ValidationError 抛出（caller 兜底转 judge_error）
    - 已经是 list / 任何其他类型 → 透传不动
    """

    def test_str_with_valid_json_list_parses(self) -> None:
        from pydantic import ValidationError

        from eval.fact_coverage_judge import _build_schemas

        _, schema_cls = _build_schemas(n_facts=2)
        # mimo 实测返回 shape：tool_call.arguments 里 verdicts 是 JSON 字符串
        json_str = (
            '[{"fact": "QPSK", "verdict": "HIT", "reason": "在"},'
            ' {"fact": "16QAM", "verdict": "MISS", "reason": "缺"}]'
        )
        try:
            obj = schema_cls.model_validate({"verdicts": json_str})
        except ValidationError as e:
            raise AssertionError(f"validator should accept JSON string: {e}") from e
        assert len(obj.verdicts) == 2
        assert obj.verdicts[0].verdict == "HIT"
        assert obj.verdicts[1].verdict == "MISS"

    def test_str_with_garbage_falls_through_to_validation_error(self) -> None:
        from pydantic import ValidationError

        from eval.fact_coverage_judge import _build_schemas

        _, schema_cls = _build_schemas(n_facts=1)
        with pytest.raises(ValidationError):
            schema_cls.model_validate({"verdicts": "not even json"})

    def test_str_with_dict_shape_falls_through(self) -> None:
        """json.loads 解出 dict（不是 list）→ 仍然 ValidationError，让 caller 兜底。"""
        from pydantic import ValidationError

        from eval.fact_coverage_judge import _build_schemas

        _, schema_cls = _build_schemas(n_facts=1)
        with pytest.raises(ValidationError):
            schema_cls.model_validate({"verdicts": '{"fact": "x", "verdict": "HIT"}'})

    def test_native_list_pass_through(self) -> None:
        """已经是 list（正常路径） → before-validator 不动，正常实例化。"""
        from eval.fact_coverage_judge import _build_schemas

        _, schema_cls = _build_schemas(n_facts=1)
        obj = schema_cls.model_validate(
            {"verdicts": [{"fact": "QPSK", "verdict": "HIT", "reason": "在"}]}
        )
        assert obj.verdicts[0].fact == "QPSK"

    def test_real_world_sample_from_2026_05_29(self) -> None:
        """复现 hand-def-001 真实失败样本（中文 fact + 句末标点）。"""
        from eval.fact_coverage_judge import _build_schemas

        _, schema_cls = _build_schemas(n_facts=1)
        # 重建 mimo 当时实际返回的 shape（节选）
        sample = (
            '[{"fact": "公共参考信号 (CRS)", "verdict": "MISS",' ' "reason": "答案未覆盖该事实。"}]'
        )
        obj = schema_cls.model_validate({"verdicts": sample})
        assert len(obj.verdicts) == 1
        assert obj.verdicts[0].verdict == "MISS"


class TestNormalizeVerdictsField:
    """生产 path：`_invoke_structured` 用 `bind_tools` + `_normalize_verdicts_field`
    手动归一化，绕过 langchain 1.4 / pydantic 2.13 上 before-validator 失效的坑。
    """

    def test_list_passes_through(self) -> None:
        from eval.fact_coverage_judge import _normalize_verdicts_field

        v = [{"fact": "X", "verdict": "HIT", "reason": "y"}]
        assert _normalize_verdicts_field(v) == v

    def test_json_str_list_parsed(self) -> None:
        from eval.fact_coverage_judge import _normalize_verdicts_field

        s = '[{"fact": "X", "verdict": "HIT", "reason": "y"}]'
        out = _normalize_verdicts_field(s)
        assert isinstance(out, list)
        assert out[0]["verdict"] == "HIT"

    def test_json_str_dict_falls_back_to_string(self) -> None:
        """json.loads 解出非 list（dict / 数字）→ 返回原 string，caller 兜底拒判。"""
        from eval.fact_coverage_judge import _normalize_verdicts_field

        s = '{"fact": "X"}'
        assert _normalize_verdicts_field(s) == s

    def test_garbage_str_returns_string(self) -> None:
        from eval.fact_coverage_judge import _normalize_verdicts_field

        assert _normalize_verdicts_field("not json") == "not json"

    def test_none_passes_through(self) -> None:
        from eval.fact_coverage_judge import _normalize_verdicts_field

        assert _normalize_verdicts_field(None) is None

    def test_real_world_2026_05_29_string_form_normalized(self) -> None:
        """2026-05-29 hand-def-003 实测的 mimo 字符串 verdict shape。"""
        from eval.fact_coverage_judge import _normalize_verdicts_field

        # 节选自 terminal log 真实 input_value
        s = '[{"fact": "传输块的", "verdict": "MISS", "reason": "未在资料中找到。"}]'
        out = _normalize_verdicts_field(s)
        assert isinstance(out, list)
        assert len(out) == 1
        assert out[0]["verdict"] == "MISS"


# === bind_tools production path（自解析 tool_call.args） ====================


class _FakeBoundChain:
    """`llm.bind_tools(...)` 返回的 chain 桩。`invoke(prompt)` 返回构造时给的
    AIMessage（带 tool_calls）或 raise。
    """

    def __init__(self, *, ai_message: Any = None, raise_exc: Exception | None = None) -> None:
        self._ai = ai_message
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def invoke(self, prompt: str) -> Any:
        if self._raise is not None:
            raise self._raise
        return self._ai


class _FakeChatBindTools:
    """ChatOpenAI 的最小桩（bind_tools 路径）；记录 bind_tools 调用参数。"""

    def __init__(self, *, ai_message: Any = None, raise_exc: Exception | None = None) -> None:
        self._chain = _FakeBoundChain(ai_message=ai_message, raise_exc=raise_exc)
        self.bind_calls: list[dict[str, Any]] = []

    def bind_tools(
        self,
        tools: list[Any],
        *,
        tool_choice: Any | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> _FakeBoundChain:
        self.bind_calls.append(
            {
                "tools": tools,
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
            }
        )
        return self._chain


def _ai_msg_with_tool_call(args: Any, *, tool_name: str = "_Schema") -> Any:
    """构造 AIMessage shape（最简）；只用 tool_calls 字段。"""

    class _Msg:
        def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
            self.tool_calls = tool_calls

    return _Msg([{"name": tool_name, "args": args, "id": "c-1", "type": "tool_call"}])


class TestProductionBindToolsPath:
    """`_invoke_structured` 改走 `bind_tools` 路径后，对 mimo 的 string verdicts
    返回需要在自解析层归一化。"""

    def test_string_verdicts_normalized_to_list(self) -> None:
        from eval.fact_coverage_judge import FactCoverageJudge

        s = '[{"fact": "QPSK", "verdict": "HIT", "reason": "在"}]'
        ai = _ai_msg_with_tool_call({"verdicts": s})
        llm = _FakeChatBindTools(ai_message=ai)
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(facts=["QPSK"]), AgentResponse(answer="QPSK"))
        assert out["score"] == pytest.approx(1.0)
        assert out["verdicts"][0]["verdict"] == "HIT"
        # 验证 bind_tools 被正确调用：tool_choice 强制 _Schema
        assert llm.bind_calls
        choice = llm.bind_calls[0]["tool_choice"]
        assert choice and choice["function"]["name"] == "_Schema"
        assert llm.bind_calls[0]["parallel_tool_calls"] is False

    def test_native_list_args_works(self) -> None:
        from eval.fact_coverage_judge import FactCoverageJudge

        ai = _ai_msg_with_tool_call(
            {"verdicts": [{"fact": "QPSK", "verdict": "MISS", "reason": "缺"}]}
        )
        llm = _FakeChatBindTools(ai_message=ai)
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(facts=["QPSK"]), AgentResponse(answer="x"))
        assert out["score"] == pytest.approx(0.0)
        assert out["verdicts"][0]["verdict"] == "MISS"

    def test_no_tool_calls_treated_as_judge_error(self) -> None:
        from eval.fact_coverage_judge import FactCoverageJudge

        class _NoToolMsg:
            def __init__(self) -> None:
                self.tool_calls: list[Any] = []

        llm = _FakeChatBindTools(ai_message=_NoToolMsg())
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(facts=["QPSK"]), AgentResponse(answer="x"))
        assert out["score"] is None
        assert "judge_error" in (out["reason"] or "")
        assert "no tool_call" in (out["reason"] or "")

    def test_args_not_dict_raises(self) -> None:
        from eval.fact_coverage_judge import FactCoverageJudge

        # tool_call.args 偶尔会是字符串（langchain 边界情况）
        ai = _ai_msg_with_tool_call("garbage-string-not-dict")
        llm = _FakeChatBindTools(ai_message=ai)
        judge = FactCoverageJudge(llm=llm)
        out = judge.score_item(_item(facts=["QPSK"]), AgentResponse(answer="x"))
        assert out["score"] is None
        assert "judge_error" in (out["reason"] or "")
