"""eval/teleqna/whitelist.py 单测。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eval.teleqna.whitelist import (
    POC_17_SPECS,
    extract_all_spec_ids,
    is_in_whitelist,
    load_spec_aliases,
    normalize_spec_id,
)


class TestPOC17Specs:
    def test_size_and_content(self) -> None:
        assert len(POC_17_SPECS) == 17
        # M2 17 篇必须全在
        for spec in (
            "23.401",
            "23.501",
            "23.502",
            "23.503",
            "24.501",
            "29.500",
            "29.501",
            "29.502",
            "29.503",
            "29.518",
            "36.213",
            "38.214",
            "38.300",
            "38.331",
            "38.401",
            "38.413",
            "38.473",
        ):
            assert spec in POC_17_SPECS

    def test_no_duplicates(self) -> None:
        # frozenset 自动去重；这里反向校验：写一个 list 数完全等于 set 大小
        items = [
            "23.401",
            "23.501",
            "23.502",
            "23.503",
            "24.501",
            "29.500",
            "29.501",
            "29.502",
            "29.503",
            "29.518",
            "36.213",
            "38.214",
            "38.300",
            "38.331",
            "38.401",
            "38.413",
            "38.473",
        ]
        assert len(items) == len(set(items)) == len(POC_17_SPECS)


class TestNormalizeSpecId:
    @pytest.mark.parametrize(
        "raw,want",
        [
            ("38.331", "38.331"),
            ("TS 38.331", "38.331"),
            ("ts38.331", "38.331"),
            ("3GPP TS 38.331 Release 17", "38.331"),
            ("38.331 v17.5.0", "38.331"),
            ("ts38.331-h60", "38.331"),
            ("23.501", "23.501"),
            ("3gpp 23.501 v17", "23.501"),
            ("Foo TS 38.331 bar", "38.331"),
            ("no spec here", None),
            ("", None),
            ("12.34", None),  # 不是 NN.NNN
        ],
    )
    def test_extract(self, raw: str, want: str | None) -> None:
        assert normalize_spec_id(raw) == want


class TestExtractAllSpecIds:
    def test_multiple_unique_ordered(self) -> None:
        text = "See TS 38.331 §5.3.5 and TS 23.501 §6.2 then again 38.331."
        assert extract_all_spec_ids(text) == ["38.331", "23.501"]

    def test_empty(self) -> None:
        assert extract_all_spec_ids("") == []
        assert extract_all_spec_ids("nothing here") == []

    def test_with_aliases_in_text(self) -> None:
        text = "ts38.331-h60 and 3GPP TS 23.501 v17 mention 24.501."
        assert extract_all_spec_ids(text) == ["38.331", "23.501", "24.501"]


class TestIsInWhitelist:
    def test_basic(self) -> None:
        assert is_in_whitelist("38.331") is True
        assert is_in_whitelist("23.501") is True
        assert is_in_whitelist("99.999") is False
        assert is_in_whitelist(None) is False
        assert is_in_whitelist("") is False

    def test_custom_whitelist(self) -> None:
        assert is_in_whitelist("38.331", whitelist=["38.331"]) is True
        assert is_in_whitelist("38.331", whitelist=["23.501"]) is False


class TestLoadSpecAliases:
    def test_default_yaml_loads_empty(self) -> None:
        # 默认 yaml 文件 aliases: {} → 空 dict（不抛异常）
        d = load_spec_aliases()
        assert isinstance(d, dict)

    def test_custom_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "alias.yaml"
        p.write_text(
            yaml.safe_dump({"aliases": {"38.331-old": "38.331", "23.501v15": "23.501"}}),
            encoding="utf-8",
        )
        d = load_spec_aliases(p)
        assert d == {"38.331-old": "38.331", "23.501v15": "23.501"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "no_such.yaml"
        assert load_spec_aliases(p) == {}
