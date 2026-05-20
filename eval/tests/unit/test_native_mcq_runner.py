"""单测 `eval.scripts.native_mcq_runner`：解析 / 单题打分 / 聚合 / mock LLM 端到端。

覆盖（M7.2 验收）：
- parse_mcq_answer / parse_correct_option：各种 LLM 输出与 TeleQnA answer 字段格式
- score_item：predicted vs correct 一致才算对
- aggregate_results：根据 per_model_results 重算 accuracy / parse_rate / errors 正确
- evaluate_model_async：mock 的 _LiteLLMChatClient 返回特定 option → 整体准确率匹配
- write_report：JSON + markdown 均落地，含模型 accuracy 数字
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.scripts.native_mcq_runner import (
    ModelAggregate,
    ModelItemResult,
    _build_messages,
    aggregate_results,
    evaluate_model_async,
    load_filtered_items,
    parse_correct_option,
    parse_mcq_answer,
    score_item,
    write_report,
)

# === parse_mcq_answer =====================================================


class TestParseMcqAnswer:
    def test_standard_prefix(self) -> None:
        assert parse_mcq_answer("ANSWER: option 3") == "option 3"

    def test_lowercase(self) -> None:
        assert parse_mcq_answer("answer: option 1") == "option 1"

    def test_no_space(self) -> None:
        assert parse_mcq_answer("Answer: Option2") == "option 2"

    def test_inline(self) -> None:
        assert parse_mcq_answer("The correct option 4 is TS 23.501.") == "option 4"

    def test_bare_digit_line(self) -> None:
        assert parse_mcq_answer("3") == "option 3"
        assert parse_mcq_answer("(3)") == "option 3"
        assert parse_mcq_answer("3.") == "option 3"

    def test_empty_or_none(self) -> None:
        assert parse_mcq_answer("") is None
        assert parse_mcq_answer("nothing valid here") is None

    def test_first_match_wins(self) -> None:
        # 模型答案"我犹豫在 option 2 和 option 3 之间，最终选 option 2"
        assert parse_mcq_answer("between option 2 and option 3 I pick option 2") == "option 2"


# === parse_correct_option =================================================


class TestParseCorrectOption:
    def test_with_text(self) -> None:
        assert parse_correct_option("option 3: TS 23.303") == "option 3"

    def test_just_option(self) -> None:
        assert parse_correct_option("option 1") == "option 1"

    def test_empty(self) -> None:
        assert parse_correct_option("") is None
        assert parse_correct_option(None) is None  # type: ignore[arg-type]


# === score_item ===========================================================


class TestScoreItem:
    def test_match(self) -> None:
        assert score_item("option 2", "option 2") is True

    def test_case_insensitive(self) -> None:
        assert score_item("OPTION 2", "option 2") is True

    def test_mismatch(self) -> None:
        assert score_item("option 1", "option 2") is False

    def test_none_predicted(self) -> None:
        assert score_item(None, "option 2") is False

    def test_none_correct(self) -> None:
        assert score_item("option 1", None) is False


# === aggregate_results ====================================================


def _r(
    *,
    item_id: str,
    model: str = "m1",
    predicted: str | None = "option 1",
    correct: str | None = "option 1",
    error: str | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> ModelItemResult:
    return ModelItemResult(
        item_id=item_id,
        model=model,
        predicted=predicted,
        correct=correct,
        is_correct=score_item(predicted, correct),
        error=error,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


class TestAggregateResults:
    def test_all_correct(self) -> None:
        per_model = {
            "m1": [_r(item_id="a"), _r(item_id="b"), _r(item_id="c")],
        }
        aggs = aggregate_results(per_model)
        assert len(aggs) == 1
        a = aggs[0]
        assert a.total == 3
        assert a.correct == 3
        assert a.parsed == 3
        assert a.accuracy == 1.0
        assert a.parse_rate == 1.0
        assert a.errors == 0

    def test_mixed(self) -> None:
        per_model = {
            "m1": [
                _r(item_id="a", predicted="option 1", correct="option 1"),  # 对
                _r(item_id="b", predicted="option 2", correct="option 1"),  # 错
                _r(item_id="c", predicted=None, correct="option 1"),  # 解析失败
                _r(item_id="d", predicted=None, correct="option 1", error="http"),  # error
            ]
        }
        aggs = aggregate_results(per_model)
        a = aggs[0]
        assert a.total == 4
        assert a.correct == 1
        assert a.parsed == 2  # a + b
        assert a.errors == 1
        assert a.accuracy == 0.25
        assert a.parse_rate == 0.5

    def test_two_models(self) -> None:
        per_model = {
            "m1": [_r(item_id="a", predicted="option 1", correct="option 1")],
            "m2": [_r(item_id="a", predicted="option 2", correct="option 1", model="m2")],
        }
        aggs = aggregate_results(per_model)
        assert {a.model for a in aggs} == {"m1", "m2"}
        accs = {a.model: a.accuracy for a in aggs}
        assert accs == {"m1": 1.0, "m2": 0.0}

    def test_empty(self) -> None:
        aggs = aggregate_results({"m1": []})
        a = aggs[0]
        assert a.total == 0
        assert a.accuracy is None
        assert a.parse_rate is None


# === _build_messages ======================================================


class TestBuildMessages:
    def test_basic(self) -> None:
        item = {
            "id": "x",
            "question": "Which TS defines PDU Session?",
            "option 1": "TS 23.501",
            "option 2": "TS 23.502",
            "option 3": "TS 38.300",
        }
        messages = _build_messages(item)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        user = messages[1]["content"]
        assert "Which TS defines PDU Session?" in user
        assert "option 1: TS 23.501" in user
        assert "ANSWER: option" in user

    def test_no_options(self) -> None:
        # 边界：filtered.jsonl 理论上不会出现，但 _build_messages 应优雅处理
        item = {"id": "x", "question": "?"}
        messages = _build_messages(item)
        assert "(none)" in messages[1]["content"]


# === evaluate_model_async （核心断言：mock LLM 返回 option → 准确率正确）====


class _FakeChatClient:
    """复刻 _LiteLLMChatClient 的最小接口；按题 id 返回预设响应。"""

    def __init__(self, *, model: str, responses_by_id: dict[str, str]) -> None:
        self.model = model
        self._responses = responses_by_id
        self.calls: list[dict[str, Any]] = []

    async def chat(self, *, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        # 从 user message 第一行里揣测 item.id（不那么稳，更稳办法是从外部传 id）；
        # 简化：测试构造的 item 把 question 当 id，prompt 里出现 question 文本
        user = messages[-1]["content"]
        # 在 user 里找一个 `Question: <id>` 行（测试 fixture 这么生成）
        for line in user.splitlines():
            if line.startswith("Question:"):
                qid = line.split("Question:", 1)[1].strip()
                content = self._responses.get(qid, "")
                self.calls.append({"qid": qid, "model": self.model})
                return {
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                }
        return {"choices": [{"message": {"content": ""}}], "usage": {}}

    async def aclose(self) -> None:
        return None


def _mcq_item(qid: str, *, correct_option: int = 1) -> dict[str, Any]:
    return {
        "id": qid,
        "question": qid,  # 让 _FakeChatClient 通过 question 行还原 id
        "option 1": "TS 23.501",
        "option 2": "TS 23.502",
        "option 3": "TS 38.300",
        "option 4": "TS 38.331",
        "answer": f"option {correct_option}: stub",
    }


@pytest.mark.asyncio
async def test_evaluate_model_async_accuracy_three_quarters() -> None:
    """三对一错 → accuracy = 0.75，parse_rate = 1.0"""
    items = [_mcq_item("q1"), _mcq_item("q2"), _mcq_item("q3"), _mcq_item("q4")]
    responses = {
        "q1": "ANSWER: option 1",  # 对
        "q2": "ANSWER: option 1",  # 对
        "q3": "ANSWER: option 1",  # 对
        "q4": "ANSWER: option 2",  # 错
    }
    client = _FakeChatClient(model="fake-llm", responses_by_id=responses)
    results, agg = await evaluate_model_async(
        items, client=client, rpm=600, concurrent=2  # 高 rpm 避免单测慢
    )
    assert agg.total == 4
    assert agg.correct == 3
    assert agg.parsed == 4
    assert agg.errors == 0
    assert agg.accuracy == 0.75
    assert agg.parse_rate == 1.0
    # 单题字段
    by_id = {r.item_id: r for r in results}
    assert by_id["q1"].is_correct is True
    assert by_id["q4"].is_correct is False
    assert by_id["q4"].predicted == "option 2"
    assert by_id["q4"].correct == "option 1"


@pytest.mark.asyncio
async def test_evaluate_model_async_handles_parse_failures() -> None:
    """LLM 输出无 option → parsed=0 / correct=0；不应 raise。"""
    items = [_mcq_item("q1"), _mcq_item("q2")]
    responses = {"q1": "I don't know.", "q2": "Maybe option-ish?"}
    client = _FakeChatClient(model="fake", responses_by_id=responses)
    results, agg = await evaluate_model_async(items, client=client, rpm=600, concurrent=2)
    assert agg.total == 2
    assert agg.parsed == 0
    assert agg.correct == 0
    assert agg.accuracy == 0.0
    assert all(r.predicted is None for r in results)


@pytest.mark.asyncio
async def test_evaluate_model_async_two_models_independent_accuracy() -> None:
    """两个模型同 items → 各自 accuracy 独立、对 _FakeChatClient.calls 各 4 次。"""
    items = [_mcq_item(f"q{i}", correct_option=1) for i in range(1, 5)]
    # m1 全对，m2 全错
    m1 = _FakeChatClient(
        model="m1",
        responses_by_id={f"q{i}": "ANSWER: option 1" for i in range(1, 5)},
    )
    m2 = _FakeChatClient(
        model="m2",
        responses_by_id={f"q{i}": "ANSWER: option 2" for i in range(1, 5)},
    )
    _, agg1 = await evaluate_model_async(items, client=m1, rpm=600, concurrent=4)
    _, agg2 = await evaluate_model_async(items, client=m2, rpm=600, concurrent=4)
    assert agg1.accuracy == 1.0
    assert agg2.accuracy == 0.0
    assert len(m1.calls) == 4
    assert len(m2.calls) == 4


# === write_report =========================================================


class TestWriteReport:
    def test_writes_both_files(self, tmp_path: Path) -> None:
        per_model = {
            "m1": [
                _r(item_id="a"),
                _r(item_id="b", predicted="option 2"),
            ]
        }
        aggs = aggregate_results(per_model)
        report_path = write_report(
            tmp_path,
            per_model_results=per_model,
            aggregates=aggs,
            input_path=Path("/fake/filtered.jsonl"),
            n_items=2,
        )
        assert report_path == tmp_path / "report.md"
        assert (tmp_path / "results.json").exists()
        data = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
        assert data["n_items"] == 2
        assert len(data["aggregates"]) == 1
        assert data["aggregates"][0]["accuracy"] == 0.5
        md = report_path.read_text(encoding="utf-8")
        assert "TeleQnA 原生 MCQ 对照评测" in md
        assert "m1" in md
        # markdown 表格里至少出现 "2" 的 total 与 accuracy 0.500
        assert "0.500" in md


# === load_filtered_items ==================================================


class TestLoadFilteredItems:
    def test_basic(self, tmp_path: Path) -> None:
        jp = tmp_path / "filtered.jsonl"
        jp.write_text(
            "\n".join(
                [
                    json.dumps({"id": "a", "question": "?"}),
                    "",  # 空行应跳过
                    json.dumps({"id": "b", "question": "?"}),
                ]
            ),
            encoding="utf-8",
        )
        items = load_filtered_items(jp)
        assert [i["id"] for i in items] == ["a", "b"]

    def test_limit(self, tmp_path: Path) -> None:
        jp = tmp_path / "filtered.jsonl"
        jp.write_text(
            "\n".join(json.dumps({"id": f"q{i}"}) for i in range(5)),
            encoding="utf-8",
        )
        items = load_filtered_items(jp, limit=3)
        assert len(items) == 3


# === ModelAggregate.accuracy edge cases ===================================


class TestModelAggregateAccuracy:
    def test_zero_total(self) -> None:
        a = ModelAggregate(model="m")
        assert a.accuracy is None
        assert a.parse_rate is None

    def test_round_trip_to_dict(self) -> None:
        a = ModelAggregate(model="m", total=10, correct=7, parsed=9, errors=1)
        d = a.to_dict()
        assert d["accuracy"] == 0.7
        assert d["parse_rate"] == 0.9
        assert d["correct"] == 7
        assert d["errors"] == 1
