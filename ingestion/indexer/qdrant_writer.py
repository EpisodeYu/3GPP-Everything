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


def collection_name_for_provider(
    provider: str, *, prefix: str | None = None, dim: int | None = None
) -> str:
    """`{prefix}_{provider}` 或 `{prefix}_{provider}_d{dim}`（M2 multidim）。

    prefix 缺省读 `QDRANT_COLLECTION_PREFIX` 或默认 `tgpp_chunks`。
    `dim` 非空时追加 `_d{dim}` 后缀，用于多维度 collection 命名。
    """
    import os

    pre = prefix or os.environ.get("QDRANT_COLLECTION_PREFIX") or "tgpp_chunks"
    base = f"{pre}_{provider}"
    return f"{base}_d{dim}" if dim is not None else base


class QdrantWriter:
    """Qdrant 写入器。

    构造：
      - client: QdrantClient；默认按 .env QDRANT_URL / QDRANT_API_KEY 自建
      - collection_name: 默认按 provider 推
      - payload_indexed_fields: 要加 keyword 索引的 payload 字段
      - upsert_batch_size: 单次 upsert 携带的 point 数

    流程：
      writer = QdrantWriter(provider="voyage", dim=2048)
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
        collection_prefix: str | None = None,
        payload_indexed_fields: Sequence[str] = DEFAULT_PAYLOAD_INDEXED_FIELDS,
        upsert_batch_size: int = DEFAULT_UPSERT_BATCH_SIZE,
    ) -> None:
        self._client = client or self._build_default_client()
        self.provider = provider
        self.dim = dim
        self.distance = distance
        self.collection_name = collection_name or collection_name_for_provider(provider)
        # 多维度模式（ensure_collections / upsert_multidim）共用同一 prefix；
        # explicit collection_name 仍优先用于单维度兼容路径。
        self._collection_prefix = collection_prefix
        self._payload_indexed_fields = tuple(payload_indexed_fields)
        self._upsert_batch_size = upsert_batch_size
        self._collection_ready = False
        # dim → collection_name 映射；ensure_collections 后填充
        self._collections_by_dim: dict[int, str] = {}

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

    def _create_payload_indexes(self, *, collection_name: str | None = None) -> None:
        target = collection_name or self.collection_name
        for field_name in self._payload_indexed_fields:
            try:
                self._client.create_payload_index(
                    collection_name=target,
                    field_name=field_name,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:  # 索引已存在时 Qdrant 抛 4xx；幂等吞掉
                msg = str(exc)
                if "already exists" in msg.lower() or "already indexed" in msg.lower():
                    continue
                # 多数 qdrant-client 版本对重复索引返回 200，少数抛；安全起见 log + skip
                log.debug("payload index create skipped (%s): %s", field_name, msg)

    # -------------------- 多维度 collection（M2 §4.7） --------------------

    def ensure_collections(self, dims: Sequence[int]) -> dict[int, str]:
        """为每个 dim 建一个 `{prefix}_{provider}_d{dim}` collection（idempotent）。

        返回 dim → collection_name 映射；后续 `upsert_multidim` 直接消费此映射。
        """
        if not dims:
            raise RuntimeError("ensure_collections: dims must be non-empty")
        out: dict[int, str] = {}
        for d in sorted({int(x) for x in dims}, reverse=True):
            name = collection_name_for_provider(
                self.provider, prefix=self._collection_prefix, dim=d
            )
            if not self._client.collection_exists(name):
                log.info(
                    "creating qdrant collection: %s (dim=%d, distance=%s)",
                    name,
                    d,
                    self.distance,
                )
                self._client.create_collection(
                    collection_name=name,
                    vectors_config=qmodels.VectorParams(size=d, distance=self.distance),
                )
            else:
                log.info("qdrant collection exists: %s", name)
            self._create_payload_indexes(collection_name=name)
            out[d] = name
        self._collections_by_dim = out
        self._collection_ready = True
        return dict(out)

    def upsert_multidim(
        self,
        chunks: Sequence[Any],
        vectors_by_dim: dict[int, Sequence[Sequence[float]]],
    ) -> dict[int, int]:
        """同一批 chunks，每个 dim 写入对应 `_d{dim}` collection；返回 dim → upserted。

        - 共用同一 chunk_id（uuid5）→ 跨 collection 可对照
        - vectors_by_dim 的每个 list 长度必须 == len(chunks)
        - 必须先调 ensure_collections(dims)；否则抛
        """
        if not self._collections_by_dim:
            raise RuntimeError("call ensure_collections(dims) before upsert_multidim()")
        missing = [d for d in vectors_by_dim if d not in self._collections_by_dim]
        if missing:
            raise RuntimeError(
                f"upsert_multidim: dim(s) {missing} not in ensure_collections; "
                f"have {sorted(self._collections_by_dim)}"
            )
        if not chunks:
            return {d: 0 for d in vectors_by_dim}

        out: dict[int, int] = {}
        for dim, name in sorted(self._collections_by_dim.items(), reverse=True):
            if dim not in vectors_by_dim:
                continue  # caller 可能只 upsert 部分 dim
            vecs = vectors_by_dim[dim]
            if len(chunks) != len(vecs):
                raise ValueError(
                    f"upsert_multidim dim={dim}: chunks/vectors length mismatch "
                    f"({len(chunks)} vs {len(vecs)})"
                )
            upserted = 0
            for start in range(0, len(chunks), self._upsert_batch_size):
                batch_chunks = chunks[start : start + self._upsert_batch_size]
                batch_vecs = vecs[start : start + self._upsert_batch_size]
                points = [
                    qmodels.PointStruct(
                        id=_ensure_qdrant_point_id(c.chunk_id),
                        vector=list(v),
                        payload=_chunk_to_payload(c),
                    )
                    for c, v in zip(batch_chunks, batch_vecs, strict=True)
                ]
                self._client.upsert(collection_name=name, points=points)
                upserted += len(points)
            log.info("qdrant multidim upsert: %d → %s", upserted, name)
            out[dim] = upserted
        return out

    def purge_spec_multidim(self, spec_id: str) -> dict[int, int]:
        """跨所有已 ensure 的 multidim collection 按 spec_id 删（重建 spec 前调）。"""
        if not self._collections_by_dim:
            return {}
        out: dict[int, int] = {}
        for dim, name in self._collections_by_dim.items():
            if not self._client.collection_exists(name):
                out[dim] = 0
                continue
            flt = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(key="spec_id", match=qmodels.MatchValue(value=spec_id))
                ]
            )
            before = int(self._client.count(name, count_filter=flt, exact=True).count)
            self._client.delete(
                collection_name=name,
                points_selector=qmodels.FilterSelector(filter=flt),
            )
            log.info("qdrant purge_spec_multidim: %s removed %d from %s", spec_id, before, name)
            out[dim] = before
        return out

    def count_multidim(self, *, spec_id: str | None = None) -> dict[int, int]:
        """返回每个 dim collection 的 point 数（按 spec_id 过滤可选）。"""
        out: dict[int, int] = {}
        for dim, name in self._collections_by_dim.items():
            if not self._client.collection_exists(name):
                out[dim] = 0
                continue
            if spec_id is None:
                out[dim] = int(self._client.count(name, exact=True).count)
            else:
                flt = qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="spec_id", match=qmodels.MatchValue(value=spec_id)
                        )
                    ]
                )
                out[dim] = int(self._client.count(name, count_filter=flt, exact=True).count)
        return out

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

    def count(self, *, spec_id: str | None = None, collection_name: str | None = None) -> int:
        """collection 总数或按 spec_id 过滤计数。

        target 选择优先级：
          1. 显式 `collection_name=` 参数
          2. 已 ensure 的 multidim collection（取主 dim = max dim）→ M2 §4.7 起的默认路径
          3. self.collection_name（兼容旧 single-dim 路径）

        这样 `pipeline_concurrent` 在 ensure_collections 之后调 `count(spec_id=...)`
        能拿到主 dim collection 的真实计数，`--skip-indexed` 不再永远命中 0。
        """
        target = collection_name
        if target is None:
            if self._collections_by_dim:
                target = self._collections_by_dim[max(self._collections_by_dim)]
            else:
                target = self.collection_name
        if not self._client.collection_exists(target):
            return 0
        if spec_id is None:
            return int(self._client.count(target, exact=True).count)
        flt = qmodels.Filter(
            must=[qmodels.FieldCondition(key="spec_id", match=qmodels.MatchValue(value=spec_id))]
        )
        return int(self._client.count(target, count_filter=flt, exact=True).count)


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
