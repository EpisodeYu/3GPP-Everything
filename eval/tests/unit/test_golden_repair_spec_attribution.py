"""Unit tests for eval.scripts.golden_repair_spec_attribution.

聚焦纯函数：spec_id 归一化、TeleQnA 记录解析、attribution 输出 sanitize。
LLM 调用走 monkeypatch stub。
"""

from __future__ import annotations

import pytest

from eval.scripts import golden_repair_spec_attribution as M


class TestNormalizeSpecs:
    def test_strips_ts_prefix(self) -> None:
        assert M._normalize_specs(["TS 23.501", "ts 24.501", "29.500"]) == [
            "23.501",
            "24.501",
            "29.500",
        ]

    def test_drops_invalid(self) -> None:
        assert M._normalize_specs(["foo", "bar", "23.501", ""]) == ["23.501"]

    def test_dedup_and_sort(self) -> None:
        assert M._normalize_specs(["29.518", "23.501", "29.518"]) == ["23.501", "29.518"]

    def test_multi_part(self) -> None:
        assert M._normalize_specs(["38.521-3", "36.521-1"]) == ["36.521-1", "38.521-3"]


class TestExtractCorrectOption:
    def test_with_colon_prefix(self) -> None:
        rec = {"answer": "option 3: TS 23.303"}
        assert M._extract_correct_option(rec) == "TS 23.303"

    def test_without_colon(self) -> None:
        rec = {"answer": "Some plain answer"}
        assert M._extract_correct_option(rec) == "Some plain answer"

    def test_missing(self) -> None:
        assert M._extract_correct_option({}) == ""


class TestCurrentExpectedSpecs:
    def test_extracts_sorted(self) -> None:
        item = {
            "expected_specs": [
                {"spec_id": "29.518"},
                {"spec_id": "23.501"},
                {"spec_id": ""},
            ]
        }
        assert M._current_expected_specs(item) == ["23.501", "29.518"]

    def test_empty(self) -> None:
        assert M._current_expected_specs({}) == []
        assert M._current_expected_specs({"expected_specs": []}) == []


class TestSpecIDRegex:
    @pytest.mark.parametrize(
        "spec,ok",
        [
            ("38.331", True),
            ("23.501", True),
            ("38.521-3", True),
            ("36.521-1", True),
            ("3.501", False),  # 3 digits required
            ("38.33", False),  # 3-digit minor
            ("38.331a", False),
            ("ts 38.331", False),  # prefix removed in caller
        ],
    )
    def test_regex(self, spec: str, ok: bool) -> None:
        assert bool(M._SPEC_ID_RE.match(spec)) is ok


class TestAttributeItemStub:
    def test_returns_normalized_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake(*args, **kwargs):
            return {
                "spec_ids": ["TS 23.501", "29.500", "junk"],
                "confidence": "HIGH",  # case-insensitive
                "reasoning": "the rationale",
            }

        monkeypatch.setattr(M, "_call_llm", fake)
        from eval.settings import EvalSettings

        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        tq = {"question": "q?", "answer": "option 1: foo", "explanation": "blah"}
        out = M.attribute_item(s, {"id": "def-x"}, tq, model="x")
        assert out is not None
        assert out["spec_ids"] == ["23.501", "29.500"]
        assert out["confidence"] == "high"
        assert out["reasoning"] == "the rationale"

    def test_unknown_confidence_falls_back_to_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake(*args, **kwargs):
            return {"spec_ids": ["23.501"], "confidence": "absolutely"}

        monkeypatch.setattr(M, "_call_llm", fake)
        from eval.settings import EvalSettings

        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        out = M.attribute_item(s, {"id": "x"}, {"answer": ""}, model="x")
        assert out is not None
        assert out["confidence"] == "low"

    def test_llm_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(M, "_call_llm", lambda *a, **k: None)
        from eval.settings import EvalSettings

        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        out = M.attribute_item(s, {"id": "x"}, {"answer": ""}, model="x")
        assert out is None
