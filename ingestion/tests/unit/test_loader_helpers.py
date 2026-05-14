"""loader 内部辅助函数 + manifest_store 端到端单测（无网络）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.hf_loader.loader import (
    _basename,
    _extract_title,
    _parse_original_filename,
    _strip_series_suffix,
)
from ingestion.hf_loader.manifest_store import (
    get_meta,
    manifest_session,
    open_manifest,
    read_entries,
    set_meta,
    write_entries,
)
from ingestion.hf_loader.models import SpecManifestEntry


class TestBaseHelpers:
    def test_basename(self):
        assert _basename("marked/Rel-19/38_series/38211") == "38211"
        assert _basename("38211") == "38211"

    def test_strip_series_suffix(self):
        assert _strip_series_suffix("38_series") == "38"
        assert _strip_series_suffix("marked/Rel-19/38_series") == "38"
        assert _strip_series_suffix("foo") == "foo"


class TestParseOriginalFilename:
    @pytest.mark.parametrize(
        "fname,uid,version",
        [
            ("38101-1-j50_cover.docx", "38101-1", "j50"),
            ("38211-j50_s00-04.docx", "38211", "j50"),
            ("21101-j00.docx", "21101", "j00"),
            ("23501-1-k10_sAll.docx", "23501-1", "k10"),
            ("foo.docx", "foo", None),
        ],
    )
    def test_parsing(self, fname, uid, version):
        out_uid, out_ver = _parse_original_filename(fname)
        assert out_uid == uid
        assert out_ver == version


class TestExtractTitle:
    def test_h1_title(self):
        assert _extract_title("# 38.211 V19.0.0\nbody\n") == "38.211 V19.0.0"

    def test_no_h1_falls_back_to_first_line(self):
        assert _extract_title("Some preamble line\nmore lines") == "Some preamble line"

    def test_empty(self):
        assert _extract_title("") is None
        assert _extract_title("\n\n\n") is None


class TestManifestStore:
    def test_round_trip(self, tmp_path: Path) -> None:
        db = tmp_path / "manifest.sqlite"
        entries = [
            SpecManifestEntry(
                spec_uid="38211",
                spec_id="38.211",
                spec_number="38.211",
                spec_type="TS",
                release="Rel-19",
                series="38",
                title="Physical channels",
                raw_md_path="marked/Rel-19/38_series/38211/raw.md",
                image_paths=("marked/Rel-19/38_series/38211/abc_img.jpg",),
                image_sizes=(12345,),
                raw_md_size=487125,
                source_doc_path="original/Rel-19/38_series/38211-j50_s00-04.docx",
                source_doc_version="j50",
                dataset_revision="deadbeef",
            ),
            SpecManifestEntry(
                spec_uid="38211",
                spec_id="38.211",
                spec_number="38.211",
                spec_type="TS",
                release="Rel-18",
                series="38",
                title="Physical channels (R18)",
                raw_md_path="marked/Rel-18/38_series/38211/raw.md",
                image_paths=(),
                image_sizes=(),
                raw_md_size=400000,
                source_doc_path=None,
                source_doc_version="i90",
                dataset_revision="deadbeef",
            ),
        ]
        with manifest_session(db) as conn:
            n = write_entries(conn, entries)
            assert n == 2
            set_meta(conn, "last_pull_revision", "deadbeef")
        # 重新打开，确认能读回来
        with manifest_session(db) as conn:
            roundtripped = read_entries(conn)
            assert len(roundtripped) == 2
            spec_ids = sorted({(e.spec_id, e.release) for e in roundtripped})
            assert spec_ids == [("38.211", "Rel-18"), ("38.211", "Rel-19")]
            assert get_meta(conn, "last_pull_revision") == "deadbeef"
            r19 = next(e for e in roundtripped if e.release == "Rel-19")
            assert r19.image_paths == ("marked/Rel-19/38_series/38211/abc_img.jpg",)
            assert r19.image_sizes == (12345,)
            assert r19.source_doc_version == "j50"

    def test_upsert_overwrites(self, tmp_path: Path) -> None:
        db = tmp_path / "manifest.sqlite"
        e0 = SpecManifestEntry(
            spec_uid="38211",
            spec_id="38.211",
            spec_number="38.211",
            spec_type="TS",
            release="Rel-19",
            series="38",
            title="old",
            raw_md_path="marked/Rel-19/38_series/38211/raw.md",
            raw_md_size=1,
            dataset_revision="r1",
        )
        e1 = SpecManifestEntry(
            spec_uid="38211",
            spec_id="38.211",
            spec_number="38.211",
            spec_type="TS",
            release="Rel-19",
            series="38",
            title="new",
            raw_md_path="marked/Rel-19/38_series/38211/raw.md",
            raw_md_size=999,
            dataset_revision="r2",
        )
        with manifest_session(db) as conn:
            write_entries(conn, [e0])
            write_entries(conn, [e1])
            roundtripped = read_entries(conn)
            assert len(roundtripped) == 1
            assert roundtripped[0].title == "new"
            assert roundtripped[0].raw_md_size == 999
            assert roundtripped[0].dataset_revision == "r2"

    def test_open_creates_schema(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.sqlite"
        conn = open_manifest(db)
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r[0] for r in cur.fetchall()]
            assert "manifest_entries" in tables
            assert "manifest_meta" in tables
            assert get_meta(conn, "schema_version") == "1"
        finally:
            conn.close()
