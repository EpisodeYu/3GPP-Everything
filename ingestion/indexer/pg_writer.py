"""PG `chunks_meta` 写入层。

docs §3.1 / §4.4 中 backend 端会维护完整 chunks_meta / documents / document_versions
三张表。但 backend alembic 当前未上线（M1 阶段先打通数据流）；ingestion 端先以
**最小子集 + idempotent CREATE TABLE IF NOT EXISTS** 维护 `chunks_meta`：

- 仅建 `chunks_meta` 一张表
- 字段以 docs §3.1 + chunker 实际输出为准；不擅自加 documents FK
- `char_offset_start` / `char_offset_end` 字段保留但 nullable（chunker small2big
  策略下不产生准确偏移）
- 类型用 SQLAlchemy 跨方言基类（PG + SQLite 都跑得起来），方便单测用 SQLite

backend alembic 上线后由 `op.create_table_if_not_exists` 接管，不会冲突。

upsert：DELETE WHERE chunk_id IN (...) + INSERT VALUES。事务内执行，简单可靠，
不依赖 PG-only 的 ON CONFLICT 语法 → 单测可用 SQLite 跑通。

provider 列：voyage / glm（双轨期分辨同 chunk 在两套索引中的状态）。同 chunk_id +
provider 视为唯一；不同 provider 共存同一行 chunk_id（按 docs §3.1，chunk_id 是
Qdrant point id，与 provider 对应一致）。

为允许同 chunk_id 在不同 provider 下并存（双轨索引），唯一约束改为
`UNIQUE (chunk_id, provider)` 而非仅 `chunk_id`。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
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
    UniqueConstraint,
    bindparam,
    create_engine,
    delete,
    insert,
    select,
    text,
)

log = logging.getLogger(__name__)

# SQLAlchemy MetaData 单例：表定义只在模块加载时跑一次
_metadata = MetaData()

chunks_meta_table = Table(
    "chunks_meta",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("chunk_id", String(64), nullable=False, index=True),
    Column("provider", String(32), nullable=False, index=True),
    Column("spec_id", String(32), nullable=False, index=True),
    Column("spec_uid", String(32), nullable=True),
    Column("spec_number", String(32), nullable=False),
    Column("spec_type", String(8), nullable=False),
    Column("release", String(16), nullable=False, index=True),
    Column("series", String(8), nullable=False, index=True),
    Column("title", String(512), nullable=False),
    Column("chunk_type", String(32), nullable=False, index=True),
    Column("clause", String(64), nullable=False, index=True),
    Column("section_path", JSON, nullable=False),
    Column("section_title", String(512), nullable=False),
    Column("parent_section_id", String(64), nullable=False, index=True),
    Column("parent_section_chars", Integer, nullable=False, default=0),
    Column("document_order", Integer, nullable=False, default=0),
    Column("char_offset_start", Integer, nullable=True),
    Column("char_offset_end", Integer, nullable=True),
    Column("raw_extra", JSON, nullable=False),
    Column("cross_refs", JSON, nullable=False),
    Column("source", String(32), nullable=False),
    Column("source_version", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("chunk_id", "provider", name="uq_chunks_meta_chunk_id_provider"),
)


def default_database_url() -> str | None:
    """读 `DATABASE_URL`；若是 backend 的 async dsn（`postgresql+asyncpg://...`），
    自动转为 psycopg3 同步 dsn（`postgresql+psycopg://...`）。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    return _to_sync_psycopg(url)


def _to_sync_psycopg(url: str) -> str:
    """`postgresql+asyncpg://` → `postgresql+psycopg://`；其他 scheme 保持原样。

    asyncpg 是 backend 的 async driver；ingestion 用同步代码 + psycopg[binary]，
    必须替换 scheme 否则 SQLAlchemy 报 ModuleNotFoundError: asyncpg。
    """
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgres+asyncpg://"):  # 偶见简写
        return "postgresql+psycopg://" + url[len("postgres+asyncpg://") :]
    return url


def build_engine(database_url: str | None = None, **kwargs: Any) -> Engine:
    """构造 SQLAlchemy Engine（同步）。

    `database_url=None` 走 .env DATABASE_URL；scheme 自动转 psycopg3。
    `pool_pre_ping=True` 防止 PG 闲置连接超时（共享实例有此风险）。
    """
    url = database_url or default_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL not set; pass database_url= explicitly or configure .env")
    return create_engine(_to_sync_psycopg(url), pool_pre_ping=True, future=True, **kwargs)


class PgChunkMetaWriter:
    """PG `chunks_meta` 写入器。

    构造：
      - engine: SQLAlchemy Engine（可由 build_engine() 构造）；测试用 in-memory SQLite
      - provider: "voyage" / "glm"
      - schema_owner: 是否在初始化时 CREATE TABLE IF NOT EXISTS（默认 True）

    幂等：同 (chunk_id, provider) 重跑会覆盖；spec 级 purge_spec 删整篇。
    """

    def __init__(
        self,
        *,
        engine: Engine,
        provider: str = "voyage",
        schema_owner: bool = True,
    ) -> None:
        self._engine = engine
        self.provider = provider
        if schema_owner:
            self.ensure_schema()

    @classmethod
    def from_env(
        cls,
        *,
        provider: str = "voyage",
        database_url: str | None = None,
        schema_owner: bool = True,
    ) -> PgChunkMetaWriter:
        engine = build_engine(database_url)
        return cls(engine=engine, provider=provider, schema_owner=schema_owner)

    def close(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> PgChunkMetaWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------- schema --------------------

    def ensure_schema(self) -> None:
        """CREATE TABLE IF NOT EXISTS chunks_meta + indexes。

        生产环境 backend alembic 起来后此方法仍可调（idempotent，PG 与 SQLite 都不会重建已存在表）。
        """
        _metadata.create_all(self._engine, tables=[chunks_meta_table], checkfirst=True)
        log.debug("pg chunks_meta schema ensured")

    # -------------------- 写入 --------------------

    def upsert_chunks(self, chunks: Sequence[Any]) -> int:
        """DELETE WHERE (chunk_id, provider) ∈ batch + INSERT VALUES，事务内执行。

        - chunk_id 列已建索引（uq_chunks_meta_chunk_id_provider 也建索引）；
          DELETE WHERE IN (...) 在 PG 走 index scan，几百行级别延迟 < 50ms
        - 大批量（如 38.331 9k chunks）按 1000 切片 commit；避免单事务过大
        """
        if not chunks:
            return 0

        batch_size = 1000
        total = 0
        with self._engine.begin() as conn:
            for start in range(0, len(chunks), batch_size):
                batch = chunks[start : start + batch_size]
                chunk_ids = [c.chunk_id for c in batch]
                conn.execute(
                    delete(chunks_meta_table)
                    .where(chunks_meta_table.c.chunk_id.in_(chunk_ids))
                    .where(chunks_meta_table.c.provider == self.provider)
                )
                rows = [self._chunk_to_row(c) for c in batch]
                conn.execute(insert(chunks_meta_table), rows)
                total += len(batch)
        log.info("pg chunks_meta upserted: provider=%s count=%d", self.provider, total)
        return total

    def purge_spec(self, spec_id: str) -> int:
        """删除该 (spec_id, provider) 所有行。

        返回被删行数（best-effort）。
        """
        with self._engine.begin() as conn:
            # SQLAlchemy 2.0：execute 返回 rowcount（PG 准确，SQLite 也支持）
            result = conn.execute(
                delete(chunks_meta_table)
                .where(chunks_meta_table.c.spec_id == spec_id)
                .where(chunks_meta_table.c.provider == self.provider)
            )
            removed = result.rowcount or 0
        log.info(
            "pg chunks_meta purge_spec: spec=%s provider=%s removed=%d",
            spec_id,
            self.provider,
            removed,
        )
        return removed

    def count(self, *, spec_id: str | None = None) -> int:
        with self._engine.connect() as conn:
            stmt = (
                select(text("count(*)"))
                .select_from(chunks_meta_table)
                .where(chunks_meta_table.c.provider == self.provider)
            )
            if spec_id is not None:
                stmt = stmt.where(chunks_meta_table.c.spec_id == spec_id)
            return int(conn.execute(stmt).scalar() or 0)

    # -------------------- 内部 --------------------

    def _chunk_to_row(self, c: Any) -> dict[str, Any]:
        """Chunk dataclass → row dict（顺序 / 命名与 chunks_meta_table 列匹配）。

        raw_extra / section_path / cross_refs 用 SQLAlchemy `JSON` 类型存：
        PG 走 native jsonb，SQLite 走 text+json1。SQLAlchemy 自动 round-trip。
        """
        return {
            "chunk_id": c.chunk_id,
            "provider": self.provider,
            "spec_id": c.spec_id,
            "spec_uid": c.spec_uid,
            "spec_number": c.spec_number,
            "spec_type": c.spec_type,
            "release": c.release,
            "series": c.series,
            "title": c.title,
            "chunk_type": c.chunk_type,
            "clause": c.clause,
            "section_path": list(c.section_path),
            "section_title": c.section_title,
            "parent_section_id": c.parent_section_id,
            "parent_section_chars": c.parent_section_chars,
            "document_order": c.document_order,
            "char_offset_start": None,
            "char_offset_end": None,
            "raw_extra": _json_clean(c.raw_extra),
            "cross_refs": list(c.cross_refs),
            "source": c.source,
            "source_version": c.source_version,
            "created_at": c.created_at,
        }


def _json_clean(obj: Any) -> Any:
    """raw_extra 内可能含 tuple / datetime / dataclass 等 PG/SQLite JSON 不直收的类型。

    与 qdrant_writer._sanitize_payload 同思路：递归转 list / str。
    """
    if isinstance(obj, dict):
        return {str(k): _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_json_clean(v) for v in obj]
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    return str(obj)


# 给单测便捷接入：拼一行 row JSON（便于断言）
def chunks_to_rows(chunks: Sequence[Any], *, provider: str) -> list[dict[str, Any]]:
    writer = PgChunkMetaWriter.__new__(PgChunkMetaWriter)
    writer.provider = provider
    return [writer._chunk_to_row(c) for c in chunks]


# 避免 ruff 报"未使用 import"：bindparam / json 给类型对齐保留
_ = bindparam
_ = json


__all__ = [
    "PgChunkMetaWriter",
    "build_engine",
    "chunks_meta_table",
    "chunks_to_rows",
    "default_database_url",
]
