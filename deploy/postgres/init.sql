-- tgpp-postgres 容器首次启动时由官方镜像 entrypoint 自动执行（落在
-- /docker-entrypoint-initdb.d/ 下，仅在 data 目录为空时跑一次）。
--
-- 锚：docs/03-development/01-infrastructure.md §2.2
--
-- 现状（2026-05-27 解耦后）：
-- - 业务 schema 由 Alembic 管理（backend/alembic/versions/*.py），不在此文件创建表。
-- - 这里只装 extension，让 Alembic migration 跑得动（init schema 用 gen_random_uuid / uuid_generate_v4）。
-- - 项目走 Qdrant 单轨，不启用 pgvector（schema 不含 vector 列；docs §2.2 历史 fallback 已废）。

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
