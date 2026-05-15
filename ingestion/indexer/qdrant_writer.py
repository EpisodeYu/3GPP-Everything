"""Qdrant 写入层。

实现 docs §4.4 中的：

- collection per provider：`{QDRANT_COLLECTION_PREFIX}_{provider}`，默认 `tgpp_chunks_voyage` /
  `tgpp_chunks_glm`
- payload 字段加 keyword 索引：`spec_number`, `release`, `series`, `clause`, `chunk_type`,
  `parent_section_id`（M3 small2big 召回需要按 parent group）
- 幂等 upsert：point id = chunk_id（uuid5），同 id 再写入 = 替换；
  chunker 已保证内容不变 → chunk_id 不变 → 真正幂等
- spec 级 purge：按 `spec_id` 过滤删除（用于"重建一篇 spec"）

QdrantClient 默认起一个 HTTP client（线程安全）；测试可注入 `QdrantClient(":memory:")`。

distance metric：voyage / glm 文档都建议 cosine；本项目主指标也是 cosine 相似度。
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

log = logging.getLogger(__name__)

DEFAULT_PAYLOAD_INDEXED_FIELDS = (
    "spec_id",
    "spec_number",
    "release",
    "series",
    "clause",
    "chunk_type",
    "parent_section_id",
)
DEFAULT_UPSERT_BATCH_SIZE = 128
DEFAULT_DISTANCE = qmodels.Distance.COSINE


def collection_name_for_provider(provider: str, *, prefix: str | None = None) -> str:
    """`{prefix}_{provider}`；prefix 缺省读 `QDRANT_COLLECTION_PREFIX` 或默认 `tgpp_chunks`。"""
    import os

    pre = prefix or os.environ.get("QDRANT_COLLECTION_PREFIX") or "tgpp_chunks"
    return f"{pre}_{provider}"


class QdrantWriter:
    """Qdrant 写入器。

    构造：
      - client: QdrantClient；默认按 .env QDRANT_URL / QDRANT_API_KEY 自建
      - collection_name: 默认按 provider 推
      - payload_indexed_fields: 要加 keyword 索引的 payload 字段
      - upsert_batch_size: 单次 upsert 携带的 point 数

    流程：
      writer = QdrantWriter(provider="voyage", dim=1024)
      writer.ensure_collection()          # idempotent
      writer.upsert_chunks(chunks, vectors)   # 批量
      writer.purge_spec("38.331")          # 删除某 spec 全部 point
    """

    def __init__(
        self,
        *,
        client: QdrantClient | None = None,
        provider: str = "voyage",
        dim: int | None = None,
        distance: qmodels.Distance = DEFAULT_DISTANCE,
        collection_name: str | None = None,
        payload_indexed_fields: Sequence[str] = DEFAULT_PAYLOAD_INDEXED_FIELDS,
        upsert_batch_size: int = DEFAULT_UPSERT_BATCH_SIZE,
    ) -> None:
        self._client = client or self._build_default_client()
        self.provider = provider
        self.dim = dim
        self.distance = distance
        self.collection_name = collection_name or collection_name_for_provider(provider)
        self._payload_indexed_fields = tuple(payload_indexed_fields)
        self._upsert_batch_size = upsert_batch_size
        self._collection_ready = False

    @staticmethod
    def _build_default_client() -> QdrantClient:
        import os

        url = os.environ.get("QDRANT_URL")
        if not url:
            raise RuntimeError("QDRANT_URL not configured")
        api_key = os.environ.get("QDRANT_API_KEY") or None
        return QdrantClient(url=url, api_key=api_key)

    def close(self) -> None:
        # 旧版 qdrant-client 无 close()；新版本闲置时也可能抛 — 一律吞掉
        with contextlib.suppress(Exception):  # pragma: no cover
            self._client.close()

    def __enter__(self) -> QdrantWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------- collection 管理 --------------------

    def ensure_collection(self, *, dim: int | None = None) -> None:
        """建 collection（若不存在）+ 给指定 payload 字段加 keyword 索引。

        - dim：优先用参数；否则用构造时的 self.dim；否则报错（要求 caller warmup embedder）
        - 已存在的 collection 不会被改 dim（防止误操作）
        """
        target_dim = dim or self.dim
        if target_dim is None:
            raise RuntimeError(
                "qdrant collection dim unknown; call embedder.warmup() first or pass dim="
            )
        if not self._client.collection_exists(self.collection_name):
            log.info(
                "creating qdrant collection: %s (dim=%d, distance=%s)",
                self.collection_name,
                target_dim,
                self.distance,
            )
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qmodels.VectorParams(size=target_dim, distance=self.distance),
            )
        else:
            log.info("qdrant collection exists: %s", self.collection_name)
        self._create_payload_indexes()
        self.dim = target_dim
        self._collection_ready = True

    def _create_payload_indexes(self) -> None:
        for field_name in self._payload_indexed_fields:
            try:
                self._client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:  # 索引已存在时 Qdrant 抛 4xx；幂等吞掉
                msg = str(exc)
                if "already exists" in msg.lower() or "already indexed" in msg.lower():
                    continue
                # 多数 qdrant-client 版本对重复索引返回 200，少数抛；安全起见 log + skip
                log.debug("payload index create skipped (%s): %s", field_name, msg)

    # -------------------- 写入 / 删除 --------------------

    def upsert_chunks(self, chunks: Sequence[Any], vectors: Sequence[Sequence[float]]) -> int:
        """批量 upsert。

        - point id：chunk_id（已是 uuid5 字符串）—— qdrant-client 接受 str / int
        - payload：spec/release/clause/chunk_type/parent_section_id + raw_extra 子集
        - 重跑同 chunk_id → 替换（含 vector + payload），真正幂等
        """
        if not self._collection_ready:
            raise RuntimeError("call ensure_collection() before upsert_chunks()")
        if len(chunks) != len(vectors):
            raise ValueError(f"chunks/vectors length mismatch: {len(chunks)} vs {len(vectors)}")
        if not chunks:
            return 0

        upserted = 0
        for start in range(0, len(chunks), self._upsert_batch_size):
            batch_chunks = chunks[start : start + self._upsert_batch_size]
            batch_vecs = vectors[start : start + self._upsert_batch_size]
            points = [
                qmodels.PointStruct(
                    id=_ensure_qdrant_point_id(c.chunk_id),
                    vector=list(v),
                    payload=_chunk_to_payload(c),
                )
                for c, v in zip(batch_chunks, batch_vecs, strict=True)
            ]
            t0 = time.time()
            self._client.upsert(collection_name=self.collection_name, points=points)
            upserted += len(points)
            log.debug(
                "qdrant upserted %d points to %s in %.2fs",
                len(points),
                self.collection_name,
                time.time() - t0,
            )
        log.info("qdrant upsert done: %d points → %s", upserted, self.collection_name)
        return upserted

    def purge_spec(self, spec_id: str) -> int:
        """按 spec_id 删除该 collection 中所有 point。

        返回被删 point 数（best-effort：先 count，再 delete）。
        重建一篇 spec 前调一次，避免旧 chunk_id（内容变了）残留。
        """
        if not self._client.collection_exists(self.collection_name):
            return 0
        flt = qmodels.Filter(
            must=[qmodels.FieldCondition(key="spec_id", match=qmodels.MatchValue(value=spec_id))]
        )
        before = self.count(spec_id=spec_id)
        self._client.delete(
            collection_name=self.collection_name,
            points_selector=qmodels.FilterSelector(filter=flt),
        )
        log.info("qdrant purge_spec: %s removed %d points", spec_id, before)
        return before

    def count(self, *, spec_id: str | None = None) -> int:
        """collection 总数或按 spec_id 过滤计数。"""
        if not self._client.collection_exists(self.collection_name):
            return 0
        if spec_id is None:
            return int(self._client.count(self.collection_name, exact=True).count)
        flt = qmodels.Filter(
            must=[qmodels.FieldCondition(key="spec_id", match=qmodels.MatchValue(value=spec_id))]
        )
        return int(self._client.count(self.collection_name, count_filter=flt, exact=True).count)


# -------------------- 辅助 --------------------


def _ensure_qdrant_point_id(chunk_id: str) -> str | int:
    """qdrant 接受 uuid 字符串或 int；chunker 的 chunk_id 已是 uuid5 string，原样返回。

    防御性：若传入既非 uuid 也非纯数字（不应该发生），强制 uuid5 化以保证 qdrant 接受。
    """
    try:
        uuid.UUID(chunk_id)
        return chunk_id
    except (ValueError, TypeError, AttributeError):
        if isinstance(chunk_id, int) or (isinstance(chunk_id, str) and chunk_id.isdigit()):
            return int(chunk_id)
        # 兜底：用 NAMESPACE_URL 算一个 uuid5，避免 qdrant 拒收（实际不会触发）
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(chunk_id)))


def _chunk_to_payload(c: Any) -> dict:
    """Chunk → Qdrant payload。

    保留检索 / 召回 / 展示所需字段：
    - spec_id / spec_number / release / series / title / clause / section_title /
      section_path / parent_section_id / parent_section_chars / document_order
    - chunk_type
    - content（M3 评测时 reranker 可直接读 payload，省一次 PG 查询）
    - raw_extra（含 image_path / vision 等）

    去掉：created_at（datetime 不便序列化，且 qdrant 用不上）。
    """
    return {
        "chunk_id": c.chunk_id,
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
        "content": c.content,
        "raw_extra": _sanitize_payload(c.raw_extra),
        "cross_refs": list(c.cross_refs),
        "source": c.source,
        "source_version": c.source_version,
    }


def _sanitize_payload(obj: Any) -> Any:
    """Qdrant payload 只接受 JSON 兼容类型；递归把 tuple → list，其他基本类型保持。"""
    if isinstance(obj, dict):
        return {str(k): _sanitize_payload(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_sanitize_payload(v) for v in obj]
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    # 兜底：转字符串避免 upsert 失败
    return str(obj)


def chunks_in_qdrant_count(writer: QdrantWriter, spec_id: str) -> int:
    """便捷别名，给 runner / pipeline 用。"""
    return writer.count(spec_id=spec_id)


def iter_collections(client: QdrantClient) -> Iterable[str]:
    """枚举 collection 名，给 status CLI 用。"""
    for c in client.get_collections().collections:
        yield c.name


__all__ = [
    "DEFAULT_DISTANCE",
    "DEFAULT_PAYLOAD_INDEXED_FIELDS",
    "DEFAULT_UPSERT_BATCH_SIZE",
    "QdrantWriter",
    "chunks_in_qdrant_count",
    "collection_name_for_provider",
    "iter_collections",
]
