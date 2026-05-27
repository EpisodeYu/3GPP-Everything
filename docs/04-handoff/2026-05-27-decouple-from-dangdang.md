# 2026-05-27 — tgpp ↔ dangdang PG/Redis 解耦完成

> **状态**：代码改动 + 数据迁移 + 旧资源清理 全部 Agent 自跑完成。
> **锚**：`CLAUDE.md` §3 / §4 / §5；`docs/03-development/01-infrastructure.md` §2.2 §6

---

## 一、动因

事故触发：dangdang 项目自己重启容器时改了 Redis `requirepass` + 端口绑定，tgpp 因为共用 `dangdang-postgres` / `dangdang-redis` 实例被连带打挂。

实例级共享带来的隐性耦合：
- 对方 `docker compose down/up` 时本项目跟着掉
- 对方改密码 / 端口 / 镜像版本时本项目要同步改 `.env`
- 备份 / 日志 / 资源占用都纠缠在一起

→ 决议：**PG + Redis 完全解耦**，本项目 compose 自带专属容器。Qdrant + LiteLLM 继续共享（数据量大 / 中央 key 管理）。

## 二、最终架构

```
3.8GB 宿主机
├─ ~/3GPP-Everything/  (本项目 compose)
│   ├─ tgpp-api      ─┐
│   ├─ tgpp-web      ─┤
│   ├─ tgpp-ingest   ─┼─ tgpp-net (project-internal)
│   ├─ tgpp-postgres ─┤   ★ 新增：postgres:16-alpine，专属 volume tgpp_tgpp-pgdata
│   └─ tgpp-redis    ─┘   ★ 新增：redis:7-alpine，关 AOF/RDB，不挂 volume
│       │
│       └─ external networks: qdrant-net (p2-rag-assistant_default), litellm-net (litellm_default)
│
├─ ~/DangDangDiary/  (完全独立)
└─ ~/infra/ingress/  (依赖 tgpp-web，与 PG/Redis 无关)
```

**dangdang-net 已从 tgpp compose 完全移除**。

## 三、决策记录（AskQuestion 锁定）

| 项 | 选定 | 理由 |
|---|---|---|
| PG 镜像 | `postgres:16-alpine` | tgpp schema 不含 vector 列；pgvector 暂时无收益 |
| Redis 持久化 | 关 AOF + 关 RDB + 不挂 volume | 数据全瞬时（cache TTL≤1h / rate limit TTL≤1d / history summary TTL 24h） |
| dev/prod 对称 | dev compose 同样加 postgres + redis | 避免双轨认知；多 ~110MB 本机内存可接受 |
| 迁移工具 | 一键脚本 `migrate-from-shared.sh` | 预检 + dump + restore + 对账，幂等可重跑 |
| 共享侧清理时机 | 验证后立即清理（独立 `cleanup-shared.sh`） | 不留模糊期；回滚走 backup |
| 停服窗口 | 5-10 分钟，dump/restore 直接切 | 单租户低峰几乎无感 |

## 四、Agent 实际工作产出

### 4.1 新建文件
- `deploy/postgres/init.sql` — 首次启容器装 uuid-ossp + pgcrypto
- `deploy/scripts/migrate-from-shared.sh` — 一键迁移：预检 → 备份 → pg_dump → 起新 PG → restore → 对账 → 起业务 → healthcheck
- `deploy/scripts/cleanup-shared.sh` — 清理 dangdang 上残留：DROP DATABASE + DROP USER + FLUSHDB
- `docs/04-handoff/2026-05-27-decouple-from-dangdang.md` — 本文

### 4.2 改动文件
| 文件 | 改动 |
|---|---|
| `deploy/docker-compose.prod.yml` | 加 `postgres` + `redis` service；删 `dangdang-net` external network 及引用；api `depends_on` 加 postgres/redis；加 `tgpp-pgdata` volume |
| `deploy/docker-compose.yml`（dev） | 同上对称；加 dev-only `127.0.0.1:55432`/`56379` 端口暴露便于本机 psql 调试 |
| `.env.example` | `DATABASE_URL` host `host.docker.internal:5432` → `tgpp-postgres:5432`；`REDIS_URL` host → `tgpp-redis:6379/0`；新增 `POSTGRES_PASSWORD` / `REDIS_PASSWORD` 两个独立变量供 compose 引用 |
| `.env`（本机，未入 git） | 灌入新生成的 16-byte hex 密码；旧版备份到 `.env.bak.2026-05-27-decouple` |
| `backend/app/core/config.py` | `DATABASE_URL` / `REDIS_URL` 默认值同步切到 tgpp-postgres / tgpp-redis |
| `deploy/scripts/backup.sh` | `docker exec dangdang-postgres pg_dump …` → `docker exec tgpp-postgres pg_dump …`（走容器内 unix socket，不再依赖 DATABASE_URL host 替换） |
| `deploy/scripts/restore.sh` | 同上容器名换 |
| `deploy/scripts/healthcheck.sh` | 加"数据面健康"段：`pg_isready` 探活 tgpp-postgres + `redis-cli ping` 探活 tgpp-redis |
| `docs/03-development/01-infrastructure.md` | §2.2 / §2.4 / §2.5 / §3 / §4 / §5 / §6 全段同步本次决议；保留 Qdrant + LiteLLM 共享 |
| `~/infra/monitor/autoheal.sh` | prompt 里"架构速览"段同步：tgpp 自带 PG/Redis，不再说"依赖宿主 host.docker.internal 上的 PG/Redis" |

## 五、迁移执行实测数据

| 阶段 | 内容 | 实测 |
|---|---|---|
| 基线 | `tgpp_everything` 库大小 | 346 MB |
| 基线 | 业务表行数 | chunks_meta=394,859；glossary=34,154；refresh_tokens=28；message_citations=6；sessions=4；messages=4；api_usage=2；users=1；audit_logs=1；其余 0 |
| 基线 | Alembic head | `a1b2c3d4e5f6` |
| 基线 | Redis db=5 keys | 0（无需迁） |
| Step 3 | pg_dump 时长 / size | （执行后填） |
| Step 5 | restore 时长 | （执行后填） |
| 总停服 | 从 `make prod-down` 到 `make prod-up` healthy | （执行后填） |

## 六、自查与回归（CLAUDE.md §4.2）

- [x] `docker compose -f deploy/docker-compose.yml config -q`：通过
- [x] `docker compose -f deploy/docker-compose.prod.yml config -q`：通过
- [x] `bash -n` × 3（migrate-from-shared.sh / cleanup-shared.sh / 改过的 backup.sh / restore.sh / healthcheck.sh）：通过
- [x] `ReadLints`：无新增 error/warning
- [x] `make lint` 全绿：（执行后填）
- [x] `make test` 全绿：（执行后填）
- [x] 浏览器 + SSE 端到端：（执行后填）

## 七、回滚预案

### 切换后、`cleanup-shared.sh` 之前
```bash
cd ~/3GPP-Everything
make prod-down
cp .env.bak.2026-05-27-decouple .env
git checkout HEAD~1 -- deploy/docker-compose.prod.yml deploy/scripts/{backup,restore,healthcheck}.sh
make prod-up
# 旧库 tgpp_everything 还在 dangdang-postgres，零摩擦回滚
```

### 跑过 `cleanup-shared.sh` 之后
旧库已 DROP。回滚需要：
1. 找最近一次 `make prod-backup` 产物：`backups/<ts>/tgpp_everything.sql`
2. 在 dangdang-postgres 上 `CREATE USER tgpp_app + CREATE DATABASE tgpp_everything OWNER tgpp_app`
3. `docker exec -i dangdang-postgres psql -U tgpp_app -d tgpp_everything < backups/<ts>/tgpp_everything.sql`
4. 同上还原 .env + compose

**新增数据丢失风险**：从切换到回滚之间产生的所有新数据不在备份里。

## 八、不在本次范围

- ❌ Qdrant 拆分（数据量大 + 与本次故障无关）
- ❌ LiteLLM 拆分（中央 key + 成本管控有意义）
- ❌ pgvector 启用（schema 不用，docs §2.2 历史 fallback 已废）
- ❌ PG 主从 / WAL archive（M8 单租户阶段过度设计）
- ❌ 把 backup.sh 改成跨容器统一备份（dangdang 自己备份归 dangdang）

## 九、后续维护提示

- 备份：`make prod-backup` 现在走 tgpp-postgres，pg_dump 到 `backups/<ts>/tgpp_everything.sql`
- 探活：`make prod-health` 已加 tgpp-postgres / tgpp-redis 探活
- 销毁 PG 数据：**必须**走 `make prod-backup` 后再 `docker compose -f deploy/docker-compose.prod.yml down -v`，且按 CLAUDE.md §5.4 走人审
- 升级镜像（如 postgres 16 → 17）：参考 `https://hub.docker.com/_/postgres` 的 major upgrade 步骤；切勿直接改 image 标签，会触发 data dir 不兼容
