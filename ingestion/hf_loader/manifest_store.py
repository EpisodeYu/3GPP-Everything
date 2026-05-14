"""manifest 持久化（SQLite）。

避免每次 `hf-pull` 都要扫一遍 HF tree（一次约 30-90s/release）。
schema 故意保持极简，未来加列直接 alembic 不到的话用 `migrate_v*` 函数处理。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import SpecManifestEntry

_SCHEMA_VERSION = 1

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS manifest_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest_entries (
    spec_uid TEXT NOT NULL,
    release TEXT NOT NULL,
    spec_id TEXT NOT NULL,
    spec_number TEXT NOT NULL,
    spec_type TEXT NOT NULL,
    series TEXT NOT NULL,
    title TEXT,
    raw_md_path TEXT NOT NULL,
    raw_md_size INTEGER NOT NULL,
    image_paths_json TEXT NOT NULL,
    image_sizes_json TEXT NOT NULL,
    source_doc_path TEXT,
    source_doc_version TEXT,
    dataset_revision TEXT NOT NULL,
    PRIMARY KEY (release, spec_uid)
);

CREATE INDEX IF NOT EXISTS idx_manifest_spec_id ON manifest_entries(spec_id);
CREATE INDEX IF NOT EXISTS idx_manifest_series ON manifest_entries(series);
"""


def open_manifest(path: str | Path) -> sqlite3.Connection:
    """打开/创建 manifest SQLite，确保 schema 已就绪。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(_CREATE_TABLES)
    conn.execute(
        "INSERT OR IGNORE INTO manifest_meta(key, value) VALUES (?, ?)",
        ("schema_version", str(_SCHEMA_VERSION)),
    )
    conn.commit()
    return conn


@contextmanager
def manifest_session(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = open_manifest(path)
    try:
        yield conn
    finally:
        conn.close()


def write_entries(
    conn: sqlite3.Connection,
    entries: Iterable[SpecManifestEntry],
    *,
    replace_revision: str | None = None,
) -> int:
    """把 entries 写入 manifest。同 (release, spec_uid) 重复时覆盖。

    若 `replace_revision` 指定，则先清空该 revision 下的所有记录再写（用于重跑）。
    返回写入条数。
    """
    rows = [
        (
            e.spec_uid,
            e.release,
            e.spec_id,
            e.spec_number,
            e.spec_type,
            e.series,
            e.title,
            e.raw_md_path,
            e.raw_md_size,
            json.dumps(list(e.image_paths)),
            json.dumps(list(e.image_sizes)),
            e.source_doc_path,
            e.source_doc_version,
            e.dataset_revision,
        )
        for e in entries
    ]
    with conn:
        if replace_revision is not None:
            conn.execute(
                "DELETE FROM manifest_entries WHERE dataset_revision = ?",
                (replace_revision,),
            )
        conn.executemany(
            """
            INSERT INTO manifest_entries(
                spec_uid, release, spec_id, spec_number, spec_type, series,
                title, raw_md_path, raw_md_size, image_paths_json, image_sizes_json,
                source_doc_path, source_doc_version, dataset_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release, spec_uid) DO UPDATE SET
                spec_id=excluded.spec_id,
                spec_number=excluded.spec_number,
                spec_type=excluded.spec_type,
                series=excluded.series,
                title=excluded.title,
                raw_md_path=excluded.raw_md_path,
                raw_md_size=excluded.raw_md_size,
                image_paths_json=excluded.image_paths_json,
                image_sizes_json=excluded.image_sizes_json,
                source_doc_path=excluded.source_doc_path,
                source_doc_version=excluded.source_doc_version,
                dataset_revision=excluded.dataset_revision
            """,
            rows,
        )
    return len(rows)


def read_entries(
    conn: sqlite3.Connection, *, release: str | None = None
) -> list[SpecManifestEntry]:
    cur = conn.cursor()
    if release is None:
        cur.execute("SELECT * FROM manifest_entries ORDER BY release, series, spec_uid")
    else:
        cur.execute(
            "SELECT * FROM manifest_entries WHERE release = ? ORDER BY series, spec_uid",
            (release,),
        )
    cols = [c[0] for c in cur.description]
    out: list[SpecManifestEntry] = []
    for row in cur.fetchall():
        record = dict(zip(cols, row, strict=True))
        out.append(
            SpecManifestEntry(
                spec_uid=record["spec_uid"],
                spec_id=record["spec_id"],
                spec_number=record["spec_number"],
                spec_type=record["spec_type"],
                release=record["release"],
                series=record["series"],
                title=record["title"],
                raw_md_path=record["raw_md_path"],
                image_paths=tuple(json.loads(record["image_paths_json"])),
                image_sizes=tuple(json.loads(record["image_sizes_json"])),
                raw_md_size=int(record["raw_md_size"]),
                source_doc_path=record["source_doc_path"],
                source_doc_version=record["source_doc_version"],
                dataset_revision=record["dataset_revision"],
            )
        )
    return out


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM manifest_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO manifest_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
