"""单测 `backend.app.agent.not_found_phrases`：双语短语 + 边界。"""

from __future__ import annotations

import pytest

from app.agent.not_found_phrases import (
    NOT_FOUND_PHRASES_EN,
    NOT_FOUND_PHRASES_ZH,
    is_not_found_answer,
)


@pytest.mark.unit
class TestIsNotFoundAnswer:
    def test_en_phrase_matched(self) -> None:
        assert is_not_found_answer("This is not found in the spec.", "en") is True

    def test_en_case_insensitive(self) -> None:
        assert is_not_found_answer("NOT SPECIFIED in 38.331.", "en") is True

    def test_en_no_phrase(self) -> None:
        text = "AMF is the access and mobility management function."
        assert is_not_found_answer(text, "en") is False

    def test_zh_phrase_matched(self) -> None:
        assert is_not_found_answer("3GPP 规范未规定该字段。", "zh") is True

    def test_zh_phrase_alternate(self) -> None:
        assert is_not_found_answer("协议中未找到相关定义。", "zh") is True

    def test_zh_no_phrase(self) -> None:
        assert is_not_found_answer("AMF 是接入与移动管理功能。", "zh") is False

    def test_empty_string(self) -> None:
        assert is_not_found_answer("", "en") is False
        assert is_not_found_answer("", "zh") is False

    def test_zh_text_with_en_language_misses(self) -> None:
        """zh 文本但 language='en' → 用 en 词表 → 应 miss（避免误判）。"""
        assert is_not_found_answer("未找到相关定义。", "en") is False

    def test_en_text_with_zh_language_misses(self) -> None:
        """en 文本但 language='zh' → 用 zh 词表 → 应 miss。"""
        assert is_not_found_answer("not found in the spec.", "zh") is False

    def test_phrase_constants_non_empty(self) -> None:
        assert len(NOT_FOUND_PHRASES_EN) >= 6
        assert len(NOT_FOUND_PHRASES_ZH) >= 6

    @pytest.mark.parametrize("p", list(NOT_FOUND_PHRASES_EN))
    def test_each_en_phrase_triggers(self, p: str) -> None:
        assert is_not_found_answer(f"Sentence containing {p} here.", "en") is True

    @pytest.mark.parametrize("p", list(NOT_FOUND_PHRASES_ZH))
    def test_each_zh_phrase_triggers(self, p: str) -> None:
        assert is_not_found_answer(f"前缀 {p} 后缀。", "zh") is True
