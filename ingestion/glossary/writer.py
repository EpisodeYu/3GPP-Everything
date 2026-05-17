"""PG `glossary` 表写入。

口径与 backend alembic `init_schema` 创建的 `glossary` 表对齐：

| 列                     | 类型             | 备注                                  |
|------------------------|------------------|---------------------------------------|
| id                     | UUID PK          | 这里写入端生成 uuid4                  |
| term                   | VARCHAR(255)     | 原文术语                              |
| normalized_term        | VARCHAR(255)     | lowercase / 内部空白压一 / 索引列     |
| definition             | TEXT             | 多段允许                              |
| spec_id                | VARCHAR(32)      | 索引列                                |
| section_path           | JSON             | list[str]，按 §3.x 写                 |
| source_chunk_meta_id   | INT，FK → chunks_meta.id | 暂留 NULL（M4.4 工具节点不需要）|
| source_revision        | VARCHAR(64)      | GSMA HF revision                      |
| created_at, updated_at | TIMESTAMPTZ      | 写入端给值，避免依赖 server_default   |

幂等策略：spec 级 DELETE-then-INSERT。同 spec 内按 ``(normalized_term, section_path)``
去重（一篇 spec 同一个 section 内一次只保留首个出现的 term），跨 spec 不去重。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    text,
)
from sqlalchemy.types import Uuid as SAUuid

from ..indexer.pg_writer import build_engine
from .extractor import GlossaryEntry

log = logging.getLogger(__name__)

_metadata = MetaData()

glossary_table = Table(
    "glossary",
    _metadata,
    Column("id", SAUuid(as_uuid=True), primary_key=True),
    Column("term", String(255), nullable=False),
    Column("normalized_term", String(255), nullable=False, index=True),
    Column("definition", Text, nullable=False),
    Column("spec_id", String(32), nullable=False, index=True),
    Column("section_path", JSON, nullable=False),
    # source_chunk_meta_id FK 在 backend alembic 中是 chunks_meta.id；
    # 单测 SQLite 用 schema_owner=True 时只建 glossary 一张，FK 用 nullable INT 即可。
    Column("source_chunk_meta_id", Integer, nullable=True),
    Column("source_revision", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class PgGlossaryWriter:
    """术语写入器。

    与 ``ingestion/indexer/pg_writer.PgChunkMetaWriter`` 同构：构造可接 Engine（测试）
    或走 ``from_env``（生产 / CLI）。``schema_owner=False`` 表示假定表已由 backend
    alembic 创建；测试用 in-memory SQLite 时传 True 即可自动建表。
    """

    def __init__(self, *, engine: Engine, schema_owner: bool = False) -> None:
        self._engine = engine
        if schema_owner:
            self.ensure_schema()

    @classmethod
    def from_env(
        cls, *, database_url: str | None = None, schema_owner: bool = False
    ) -> PgGlossaryWriter:
        engine = build_engine(database_url)
        return cls(engine=engine, schema_owner=schema_owner)

    def close(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> PgGlossaryWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------- schema --------------------

    def ensure_schema(self) -> None:
        _metadata.create_all(self._engine, tables=[glossary_table], checkfirst=True)
        log.debug("pg glossary schema ensured")

    # -------------------- 写入 --------------------

    def upsert_spec(self, spec_id: str, entries: Sequence[GlossaryEntry]) -> int:
        """spec 级 DELETE-then-INSERT 幂等替换。

        返回最终写入行数（已去重）。``entries`` 为空时仅 DELETE，不 INSERT。
        """
        deduped = _dedupe_entries(entries)
        with self._engine.begin() as conn:
            conn.execute(delete(glossary_table).where(glossary_table.c.spec_id == spec_id))
            if not deduped:
                return 0
            now = datetime.now(UTC)
            rows = [self._entry_to_row(e, now=now) for e in deduped]
            conn.execute(insert(glossary_table), rows)
        log.info("pg glossary upserted: spec=%s rows=%d", spec_id, len(deduped))
        return len(deduped)

    def purge_spec(self, spec_id: str) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(glossary_table).where(glossary_table.c.spec_id == spec_id)
            )
            return int(result.rowcount or 0)

    def count(self, *, spec_id: str | None = None) -> int:
        with self._engine.connect() as conn:
            stmt = select(text("count(*)")).select_from(glossary_table)
            if spec_id is not None:
                stmt = stmt.where(glossary_table.c.spec_id == spec_id)
            return int(conn.execute(stmt).scalar() or 0)

    def find_by_normalized(self, normalized_term: str) -> list[dict[str, Any]]:
        """按 ``normalized_term`` 精确匹配，跨 spec 返回所有命中。"""
        with self._engine.connect() as conn:
            stmt = select(glossary_table).where(
                glossary_table.c.normalized_term == normalized_term
            )
            return [dict(row._mapping) for row in conn.execute(stmt).fetchall()]

    # -------------------- 内部 --------------------

    @staticmethod
    def _entry_to_row(entry: GlossaryEntry, *, now: datetime) -> dict[str, Any]:
        return {
            "id": uuid.uuid4(),
            "term": entry.term[:255],
            "normalized_term": entry.normalized_term[:255],
            "definition": entry.definition,
            "spec_id": entry.spec_id,
            "section_path": list(entry.section_path),
            "source_chunk_meta_id": None,
            "source_revision": entry.source_revision,
            "created_at": now,
            "updated_at": now,
        }


def _dedupe_entries(entries: Sequence[GlossaryEntry]) -> list[GlossaryEntry]:
    """同 ``(normalized_term, section_path)`` 仅保留首次出现的 entry。"""
    seen: set[tuple[str, tuple[str, ...]]] = set()
    out: list[GlossaryEntry] = []
    for entry in entries:
        key = (entry.normalized_term, tuple(entry.section_path))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


__all__ = [
    "PgGlossaryWriter",
    "glossary_table",
]
