# `chunks_meta` schema diff 报告（一次性，不入 CI）

- **A = `ingestion.indexer.pg_writer.chunks_meta_table`**（运行时写入侧；
  生产 PG 中现有 394,859 行数据均按这套 schema 入库）
- **B = `backend/alembic/versions/20260517_0737_9cf40059f3b1_init_schema.py`**
  （alembic init 期望侧；仅在干净 PG 上跑过 upgrade head）

差异分级：
- ❌ 硬差异：影响业务 / 数据兼容 → 触发 CLAUDE.md §5.1 上报，不进 M4.6
- ⚠️ 软差异：默认值 / PK 名 / 索引名 / autoincrement 元数据 → 不影响业务，登记即可

## 1. 列集合

- ✅ 双方列名完全一致（共 24 列）

## 2. 各列字段比对

| 列名 | 字段 | ingestion | alembic | 状态 |
|---|---|---|---|---|
| `created_at` | server_default | `None` | `now()` | ⚠️ |
| `document_order` | client_default | `0` | `None` | ⚠️ |
| `parent_section_chars` | client_default | `0` | `None` | ⚠️ |

## 3. 约束（PK / UNIQUE / FK）

- ✅ 双方约束完全一致：['PRIMARY KEY(id)', 'UNIQUE(chunk_id,provider)']

## 4. 索引（按列覆盖去重，忽略名字）

- ✅ 双方索引（列覆盖 + 名字）完全一致（共 8 个）

---

## 结论

- ✅ 关键 schema 一致（0 ❌，3 ⚠️）
- 已有 ingestion 数据的 PG 上沿用 alembic 接管**不会丢字段、不会改类型**
- 软差异（如有）仅影响首次写入的默认值或元数据命名，不动业务
- 归档完毕；M4.6 可以放心启动
