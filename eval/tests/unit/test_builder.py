"""eval/builder/transform.py 单测（不调 LLM）。

覆盖：
- _validate_and_normalize：accept / 各种 skip / whitelist 过滤 / 字段截断 /
  negative 自动 must_say_not_found
- _assign_item_id：分类前缀正确
- transform_batch_async：fake client → happy path + skip + failed + YAML 输出
- prompts: build_transform_messages 形状
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from eval.builder.prompts import (
    VALID_CATEGORIES,
    build_transform_messages,
)
from eval.builder.transform import (
    _assign_item_id,
    _LiteLLMChatClient,
    _validate_and_normalize,
    transform_batch_async,
)

# ----------------- _validate_and_normalize -----------------


class TestValidateAndNormalize:
    def _base(self, **overrides: Any) -> dict:
        body = {
            "rewritten_question": "What is PDU Session in 5G System?",
            "expected_specs": [{"spec_id": "23.501", "sections": ["3.1"]}],
            "expected_facts": ["association between", "UE and a DN", "Data Network"],
            "forbidden": ["4G"],
            "category": "definition",
            "must_say_not_found": False,
            "language": "en",
            "notes": "PDU Session is a 5G concept",
            "skip_reason": None,
        }
        body.update(overrides)
        return body

    def test_happy_path(self) -> None:
        item, skip = _validate_and_normalize(self._base())
        assert skip is None
        assert item is not None
        assert item["category"] == "definition"
        assert item["expected_specs"][0]["spec_id"] == "23.501"
        assert item["expected_facts"] == ["association between", "UE and a DN", "Data Network"]
        assert item["forbidden"] == ["4G"]
        assert item["notes"] == "PDU Session is a 5G concept"
        assert "must_say_not_found" not in item  # 只有 negative 才写

    def test_llm_signaled_skip(self) -> None:
        _, skip = _validate_and_normalize(self._base(skip_reason="exclusion-MCQ"))
        assert skip == "exclusion-MCQ"

    def test_empty_question_skipped(self) -> None:
        _, skip = _validate_and_normalize(self._base(rewritten_question=""))
        assert skip == "empty-rewritten-question"

    def test_invalid_category(self) -> None:
        _, skip = _validate_and_normalize(self._base(category="bogus"))
        assert skip and skip.startswith("invalid-category")

    def test_specs_must_be_list(self) -> None:
        _, skip = _validate_and_normalize(self._base(expected_specs="not a list"))
        assert skip == "expected_specs-not-list"

    def test_specs_outside_whitelist_dropped(self) -> None:
        # 一个 in whitelist + 一个 out + dup
        body = self._base(
            expected_specs=[
                {"spec_id": "23.501", "sections": ["3.1"]},
                {"spec_id": "22.011", "sections": []},  # 不在 whitelist
                {"spec_id": "23.501", "sections": ["6"]},  # dup
            ]
        )
        item, _ = _validate_and_normalize(body)
        assert item is not None
        assert len(item["expected_specs"]) == 1
        assert item["expected_specs"][0]["spec_id"] == "23.501"
        # 注意：sections 取第一次出现的 (dedupe by spec_id)
        assert item["expected_specs"][0]["sections"] == ["3.1"]

    def test_no_whitelist_spec_non_negative_skipped(self) -> None:
        body = self._base(
            expected_specs=[{"spec_id": "22.011", "sections": []}],
            category="procedure",
        )
        _, skip = _validate_and_normalize(body)
        assert skip == "no-whitelist-spec-and-not-negative"

    def test_negative_category_allows_no_specs(self) -> None:
        body = self._base(
            expected_specs=[],
            expected_facts=[],
            category="negative",
        )
        item, skip = _validate_and_normalize(body)
        assert skip is None
        assert item is not None
        assert item["expected_specs"] == []
        # negative 时自动 must_say_not_found
        assert item.get("must_say_not_found") is True

    def test_facts_below_min_skipped(self) -> None:
        body = self._base(expected_facts=["only one"])
        _, skip = _validate_and_normalize(body)
        assert skip == "facts<3"

    def test_facts_above_max_truncated(self) -> None:
        body = self._base(expected_facts=[f"fact-{i}" for i in range(10)])
        item, _ = _validate_and_normalize(body)
        assert item is not None
        assert len(item["expected_facts"]) == 7  # MAX_FACTS

    def test_forbidden_capped(self) -> None:
        body = self._base(forbidden=["a", "b", "c", "d", "e"])
        item, _ = _validate_and_normalize(body)
        assert item is not None
        assert len(item["forbidden"]) == 3

    def test_invalid_language_defaults_en(self) -> None:
        body = self._base(language="ja")
        item, _ = _validate_and_normalize(body)
        assert item is not None
        assert item["language"] == "en"

    def test_long_notes_truncated(self) -> None:
        body = self._base(notes="x" * 500)
        item, _ = _validate_and_normalize(body)
        assert item is not None
        assert len(item["notes"]) == 300

    def test_spec_id_ts_prefix_normalized(self) -> None:
        body = self._base(expected_specs=[{"spec_id": "TS 23.501", "sections": ["3.1"]}])
        item, _ = _validate_and_normalize(body)
        assert item is not None
        assert item["expected_specs"][0]["spec_id"] == "23.501"

    def test_sections_string_normalized_to_list(self) -> None:
        body = self._base(expected_specs=[{"spec_id": "23.501", "sections": "3.1"}])
        item, _ = _validate_and_normalize(body)
        assert item is not None
        assert item["expected_specs"][0]["sections"] == ["3.1"]


class TestAssignItemId:
    @pytest.mark.parametrize(
        "category,idx,want",
        [
            ("definition", 1, "def-001"),
            ("procedure", 5, "proc-005"),
            ("multi_section", 12, "multi-012"),
            ("table_lookup", 3, "table-003"),
            ("formula", 7, "form-007"),
            ("tool", 99, "tool-099"),
            ("negative", 100, "neg-100"),
            ("anything-else", 1, "qa-001"),
        ],
    )
    def test_format(self, category: str, idx: int, want: str) -> None:
        assert _assign_item_id(category, idx) == want


# ----------------- prompts -----------------


class TestBuildTransformMessages:
    def test_shape(self) -> None:
        msgs = build_transform_messages(
            question="What is X?",
            options={"option 1": "A", "option 2": "B"},
            answer="option 1: A",
            explanation="X is A.",
            inferred_specs=["23.501"],
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        # whitelist 主题应出现在 system prompt 里
        assert "23.501" in msgs[0]["content"]
        # user 应含 hint
        assert "23.501" in msgs[1]["content"]
        assert "What is X?" in msgs[1]["content"]
        # options 块
        assert "A" in msgs[1]["content"]

    def test_no_inferred_hint(self) -> None:
        msgs = build_transform_messages(
            question="Q",
            options={},
            answer="",
            explanation="",
            inferred_specs=None,
        )
        assert "(none)" in msgs[1]["content"]


class TestValidCategoriesConstant:
    def test_negative_in(self) -> None:
        assert "negative" in VALID_CATEGORIES
        assert "definition" in VALID_CATEGORIES
        assert "procedure" in VALID_CATEGORIES
        assert len(VALID_CATEGORIES) == 7


# ----------------- transform_batch_async (fake client) -----------------


class _FakeChatClient(_LiteLLMChatClient):
    def __init__(self, responses_by_id: dict[str, Any]) -> None:
        self._responses = responses_by_id
        self.model = "fake-mimo-pro"
        self.calls: list[str] = []

    async def aclose(self) -> None:
        pass

    async def chat(  # type: ignore[override]
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        user = next(m["content"] for m in messages if m["role"] == "user")
        # "Question: <text>" → 第一个词为 key
        first = user.split("Question:", 1)[1].strip().splitlines()[0].strip()
        key = first.split()[0]
        self.calls.append(key)
        if key not in self._responses:
            raise RuntimeError(f"no fake response for {key}")
        return self._responses[key]


def _llm_resp(body: dict) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps(body)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200},
    }


def _cand(id_: str, question: str) -> dict:
    return {
        "id": id_,
        "question": question,
        "option 1": "A",
        "option 2": "B",
        "answer": "option 1: A",
        "explanation": "ex",
        "category": "Standards specifications",
        "llm_in_whitelist": ["23.501"],
    }


class TestTransformBatchAsync:
    @pytest.mark.asyncio
    async def test_happy_path_writes_yaml(self, tmp_path: Path) -> None:
        candidates = [
            _cand("q1", "q1text about PDU"),
            _cand("q2", "q2text about Registration"),
        ]
        responses = {
            "q1text": _llm_resp(
                {
                    "rewritten_question": "What is PDU Session?",
                    "expected_specs": [{"spec_id": "23.501", "sections": ["3.1"]}],
                    "expected_facts": ["association", "UE", "DN"],
                    "forbidden": [],
                    "category": "definition",
                    "must_say_not_found": False,
                    "language": "en",
                    "notes": "",
                    "skip_reason": None,
                }
            ),
            "q2text": _llm_resp(
                {
                    "rewritten_question": "Describe Registration procedure",
                    "expected_specs": [{"spec_id": "23.502", "sections": ["4.2.2"]}],
                    "expected_facts": ["Registration Request", "AMF", "AUSF"],
                    "forbidden": [],
                    "category": "procedure",
                    "language": "en",
                    "skip_reason": None,
                }
            ),
        }
        client = _FakeChatClient(responses)
        out_yaml = tmp_path / "v1.draft.yaml"
        stats = await transform_batch_async(
            candidates,
            out_yaml=out_yaml,
            client=client,  # type: ignore[arg-type]
            rpm=600,
            concurrent=4,
            progress_every=10,
        )

        assert stats.total == 2
        assert stats.accepted == 2
        assert stats.skipped == 0
        assert stats.failed == 0
        assert stats.by_category == {"definition": 1, "procedure": 1}
        assert stats.by_spec == {"23.501": 1, "23.502": 1}

        doc = yaml.safe_load(out_yaml.read_text())
        assert doc["version"] == 1
        assert doc["total"] == 2
        items = doc["items"]
        assert len(items) == 2
        # 验 id 分配 (按 category 排序)
        ids = [it["id"] for it in items]
        assert "def-001" in ids
        assert "proc-001" in ids

    @pytest.mark.asyncio
    async def test_skip_and_fail_paths(self, tmp_path: Path) -> None:
        candidates = [
            _cand("q1", "q1ok PDU"),
            _cand("q2", "q2skip"),  # LLM 主动 skip
            _cand("q3", "q3bad"),  # JSON 解析失败
        ]
        responses: dict[str, Any] = {
            "q1ok": _llm_resp(
                {
                    "rewritten_question": "What is PDU?",
                    "expected_specs": [{"spec_id": "23.501", "sections": ["3.1"]}],
                    "expected_facts": ["a", "b", "c"],
                    "forbidden": [],
                    "category": "definition",
                }
            ),
            "q2skip": _llm_resp(
                {
                    "skip_reason": "exclusion-MCQ",
                    "rewritten_question": "",
                    "expected_specs": [],
                    "expected_facts": [],
                    "category": "definition",
                }
            ),
            "q3bad": {
                "choices": [{"message": {"content": "no json here, just chatter"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            },
        }
        client = _FakeChatClient(responses)
        out_yaml = tmp_path / "v1.draft.yaml"
        stats = await transform_batch_async(
            candidates,
            out_yaml=out_yaml,
            client=client,  # type: ignore[arg-type]
            rpm=600,
            concurrent=4,
        )
        assert stats.total == 3
        assert stats.accepted == 1
        assert stats.skipped == 1
        assert stats.failed == 1
        assert stats.skip_reasons.get("exclusion-MCQ") == 1

        skipped = (out_yaml.parent / "v1.skipped.jsonl").read_text().splitlines()
        failed = (out_yaml.parent / "v1.failed.jsonl").read_text().splitlines()
        assert len(skipped) == 1
        assert len(failed) == 1
        sk = json.loads(skipped[0])
        assert sk["item_id"] == "q2"
        assert sk["skip_reason"] == "exclusion-MCQ"
