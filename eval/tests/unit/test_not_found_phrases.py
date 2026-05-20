"""单测 `eval.not_found_phrases` + 验证与 backend 镜像保持一致。"""

from __future__ import annotations

import pytest

from eval.not_found_phrases import (
    NOT_FOUND_PHRASES_EN,
    NOT_FOUND_PHRASES_ZH,
    is_not_found_answer,
)


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

    def test_zh_no_phrase(self) -> None:
        assert is_not_found_answer("AMF 是接入与移动管理功能。", "zh") is False

    def test_empty(self) -> None:
        assert is_not_found_answer("", "en") is False
        assert is_not_found_answer("", "zh") is False

    def test_lang_does_not_cross_phrase_table(self) -> None:
        """zh 内容但 language=en → en 词表 → miss（不应误判）。"""
        assert is_not_found_answer("未找到", "en") is False
        assert is_not_found_answer("not found", "zh") is False

    @pytest.mark.parametrize("p", list(NOT_FOUND_PHRASES_EN))
    def test_each_en_phrase_triggers(self, p: str) -> None:
        assert is_not_found_answer(f"Sentence containing {p} here.", "en") is True

    @pytest.mark.parametrize("p", list(NOT_FOUND_PHRASES_ZH))
    def test_each_zh_phrase_triggers(self, p: str) -> None:
        assert is_not_found_answer(f"前缀 {p} 后缀。", "zh") is True


def test_mirror_with_backend_module() -> None:
    """eval/not_found_phrases.py 必须与 backend/app/agent/not_found_phrases.py 完全一致。

    若改了一侧未改另一侧 → 本测试 fail，强制同步。
    """
    backend_path = (
        __import__("pathlib").Path(__file__).resolve().parents[3]
        / "backend"
        / "app"
        / "agent"
        / "not_found_phrases.py"
    )
    if not backend_path.exists():
        pytest.skip(f"backend 镜像未找到: {backend_path}")

    backend_src = backend_path.read_text(encoding="utf-8")
    # 关键：两边的两个 tuple 字面量 + is_not_found_answer 函数体必须一致
    # 用 substring 比对常量定义块
    for line in (
        'NOT_FOUND_PHRASES_EN: tuple[str, ...] = (',
        '"not found",',
        '"not specified",',
        '"no such",',
        '"does not define",',
        '"is not defined in",',
        '"outside the scope",',
        'NOT_FOUND_PHRASES_ZH: tuple[str, ...] = (',
        '"未找到",',
        '"未定义",',
        '"规范未规定",',
        '"不涉及",',
        '"不在范围内",',
        '"没有相关规定",',
    ):
        assert line in backend_src, f"backend 镜像缺常量行: {line!r}"
