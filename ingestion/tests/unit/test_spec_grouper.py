"""spec_grouper 纯函数单测：spec_uid 解析、版本号、去重、白名单过滤。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ingestion.hf_loader.spec_grouper import (
    TS_5G_SERIES_WHITELIST,
    dedupe_keep_latest,
    filter_ts_5g,
    parse_doc_version,
    parse_spec_uid,
    release_rank,
)


class TestParseSpecUid:
    @pytest.mark.parametrize(
        "uid,series,spec_id",
        [
            ("38211", "38", "38.211"),
            ("23501", "23", "23.501"),
            ("38101-1", "38", "38.101-1"),
            ("23501-1", "23", "23.501-1"),
            ("36213", "36", "36.213"),
            ("21101", "21", "21.101"),
        ],
    )
    def test_standard_forms(self, uid, series, spec_id):
        s, sid, normalized = parse_spec_uid(uid)
        assert s == series
        assert sid == spec_id
        assert normalized == uid

    def test_unparseable_returns_passthrough(self):
        s, sid, normalized = parse_spec_uid("garbage_uid")
        assert s == ""
        assert sid == "garbage_uid"
        assert normalized == "garbage_uid"


class TestParseDocVersion:
    @pytest.mark.parametrize(
        "fname,expected",
        [
            ("38101-1-j50_cover.docx", "j50"),
            ("21101-j00.docx", "j00"),
            ("38211-i90_s00-04.docx", "i90"),
            ("23501-1-k10_sAll.docx", "k10"),
            ("no_version_here.docx", None),
            ("38211.docx", None),
        ],
    )
    def test_extract(self, fname, expected):
        assert parse_doc_version(fname) == expected

    def test_case_insensitive(self):
        # 文件名混合大小写也能解析；返回值统一小写
        assert parse_doc_version("38211-J50_S00.DOCX") == "j50"


class TestReleaseRank:
    def test_rank_ordering(self):
        assert release_rank("Rel-19") > release_rank("Rel-18")
        assert release_rank("Rel-18") > release_rank("Rel-17")
        assert release_rank("Rel-20") > release_rank("Rel-19")
        assert release_rank("Rel-8") < release_rank("Rel-9")

    def test_unknown_returns_negative(self):
        assert release_rank("foo") == -1
        assert release_rank("Rel-") == -1


# ---------- dedupe / filter ----------
@dataclass
class _FakeEntry:
    spec_id: str
    release: str
    spec_type: str
    series: str


class TestDedupeKeepLatest:
    def test_keeps_newer_release(self):
        entries = [
            _FakeEntry("38.211", "Rel-18", "TS", "38"),
            _FakeEntry("38.211", "Rel-19", "TS", "38"),
            _FakeEntry("23.501", "Rel-18", "TS", "23"),
        ]
        out = dedupe_keep_latest(entries)
        spec_ids = {(e.spec_id, e.release) for e in out}
        assert spec_ids == {("38.211", "Rel-19"), ("23.501", "Rel-18")}

    def test_keeps_order_of_first_appearance(self):
        # 第一次出现 38.211 是 Rel-18，第一次出现 23.501 是 Rel-19；保留出现顺序
        entries = [
            _FakeEntry("38.211", "Rel-18", "TS", "38"),
            _FakeEntry("23.501", "Rel-19", "TS", "23"),
            _FakeEntry("38.211", "Rel-19", "TS", "38"),  # 覆盖 38.211 到 Rel-19
        ]
        out = dedupe_keep_latest(entries)
        assert [e.spec_id for e in out] == ["38.211", "23.501"]
        assert [e.release for e in out] == ["Rel-19", "Rel-19"]

    def test_empty(self):
        assert dedupe_keep_latest([]) == []


class TestFilterTs5G:
    def test_drops_tr(self):
        entries = [
            _FakeEntry("38.211", "Rel-19", "TS", "38"),
            _FakeEntry("38.913", "Rel-19", "TR", "38"),
        ]
        out = filter_ts_5g(entries)
        assert [e.spec_id for e in out] == ["38.211"]

    def test_drops_non_whitelist_series(self):
        entries = [
            _FakeEntry("25.123", "Rel-19", "TS", "25"),  # 不在白名单
            _FakeEntry("41.123", "Rel-18", "TS", "41"),  # 不在白名单
            _FakeEntry("38.211", "Rel-19", "TS", "38"),
        ]
        out = filter_ts_5g(entries)
        assert {e.spec_id for e in out} == {"38.211"}

    def test_whitelist_covers_expected_series(self):
        assert "21" in TS_5G_SERIES_WHITELIST
        assert "38" in TS_5G_SERIES_WHITELIST
        assert "25" not in TS_5G_SERIES_WHITELIST
        assert "41" not in TS_5G_SERIES_WHITELIST
