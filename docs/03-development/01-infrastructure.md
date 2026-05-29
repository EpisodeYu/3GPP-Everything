# 03·01 - 基础设施

> 一切代码工作之前必须搞定的事。包括磁盘扩容、本机共享服务接入、项目骨架、环境变量、Docker Compose 雏形。

## 1. 交付物

- ✅ `/data` 可用空间 ≥ 50 GB（推荐 +50GB；低于 30GB 不进入全量索引；POC embedding 对比期默认串行跑、跑完即清）
- ✅ 本机已运行的 Qdrant / PostgreSQL / Redis / LiteLLM 完成"本项目专属命名空间"（独立 db / collection / Redis db number / LiteLLM key）
- ✅ 项目仓库目录骨架（按 `00-overview.md §3`）
- ✅ `.env.example` 完整、`.env` 本地填充验证可用
- ✅ `deploy/docker-compose.yml`（dev 版本）能 up 起 backend 占位容器、`docker compose down` 干净
- ✅ Makefile 常用命令可用：`make dev`, `make lint`, `make test`, `make ingest-poc`
- ✅ Python `uv` 工具就绪，`backend/pyproject.toml` 基础依赖锁定

## 2. 任务拆解

### 2.1 磁盘扩容

- 用户向云厂商扩容（推荐 +50GB，最低 +30GB）→ 挂到 `/dev/vda2` 或新分区 `/data`
- 如新分区：mount 到 `/data`，将 Docker volume root 迁移过去：
  ```
  /etc/docker/daemon.json:
  { "data-root": "/data/docker" }
  ```
- 验收：`df -h` 显示 `/data`（或承载 Docker/Qdrant/项目数据的挂载点）可用 ≥ 50GB；M3 决胜（2026-05-16）后已 drop 2048 collection，稳态生产 collection ~2.5GB

### 2.2 数据面服务（PG / Redis 自营 + Qdrant / LiteLLM 共享）

> 2026-05-27 解耦决议：PG/Redis 从共享 dangdang 实例迁出，由本项目 compose 自营容器（`tgpp-postgres` / `tgpp-redis`）。理由：避免对方重启/改密码/改端口连带影响本项目。Qdrant + LiteLLM 数据量大或需中央 key 管理，继续共享。详见 `docs/04-handoff/2026-05-27-decouple-from-dangdang.md`。

**PostgreSQL（自营容器 `tgpp-postgres`）**：

- 镜像 `postgres:16-alpine`（不用 pgvector：本项目走 Qdrant 单轨，schema 无 vector 列）
- 容器内 DB `tgpp_everything`、角色 `tgpp_app`、密码取 `.env` 的 `POSTGRES_PASSWORD`
- 扩展由 `deploy/postgres/init.sql` 在容器首次启动时装：

  ```sql
  CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
  CREATE EXTENSION IF NOT EXISTS "pgcrypto";
  ```

- 数据卷：`tgpp_tgpp-pgdata`（compose project=tgpp 前缀）；备份走 `pg_dump`，不直接 tar volume
- 仅 `expose` 到 `tgpp-net`，不 publish 到宿主公网；dev 额外暴露 `127.0.0.1:55432` 方便本机 psql 调试

**Qdrant（仍复用宿主 :6333）**：

- 无 db 概念，用 collection 命名隔离
- 启动期不需要预创建，由 `ingestion.indexer` 在首次写入时创建
- 检查 Qdrant 是否启用了 API key：`curl -s 127.0.0.1:6333/`；如启用则在 `.env` 配置

**Redis（自营容器 `tgpp-redis`）**：

- 镜像 `redis:7-alpine`
- 数据全瞬时（retrieval cache TTL ≤ 1h、rate limit TTL ≤ 1d、history summary TTL 24h）→ 关 AOF + 关 RDB + 不挂 volume
- requirepass 取 `.env` 的 `REDIS_PASSWORD`；db 编号统一用 `0`（自家实例无须避让）
- 仅 `expose` 到 `tgpp-net`；dev 额外暴露 `127.0.0.1:56379`

**LiteLLM（仍复用宿主 :4000）**：

- 已有 `LITELLM_MASTER_KEY` —— 项目复用同 key，或在 LiteLLM 配置内新建一个 virtual key 限制只能访问本项目用到的 model
- 项目内访问地址：`http://127.0.0.1:4000/v1`（host 直跑）或 `http://litellm:4000/v1`（容器内通过 `litellm-net` 外部网络的容器名直连）

### 2.3 项目目录骨架

按 `00-overview.md §3` 创建：

```bash
mkdir -p backend/app/{api,core,db,schemas,services,agent,retrieval,tools,llm}
mkdir -p backend/alembic backend/tests/{unit,integration,eval}
mkdir -p ingestion/{crawler,parser,chunker,indexer}
mkdir -p frontend/lib/{core,data,domain,features/{chat,reader,admin,auth}}
mkdir -p eval/golden
mkdir -p deploy/{nginx,scripts}
mkdir -p .github/workflows
```

每个 Python 包加空 `__init__.py`。

### 2.4 `.env` 规范

`/.env.example`：

```dotenv
# === 环境 ===
APP_ENV=dev                       # dev / prod
APP_DEBUG=true
APP_TIMEZONE=Asia/Shanghai
APP_SECRET_KEY=                   # JWT signing, openssl rand -hex 32

# === API listen ===
API_HOST=0.0.0.0
API_PORT=8002

# === LiteLLM (本机) ===
LITELLM_BASE_URL=http://host.docker.internal:4000/v1
LITELLM_API_KEY=

# 模型名（与 LiteLLM config.yaml 中的 model_name 一致）
LLM_AGENT_MODEL=mimo-v2.5-pro
LLM_LIGHT_MODEL=mimo-v2.5
LLM_VISION_MODEL=mimo-v2.5       # reasoning 模型；与 omni 同价但 1M 上下文。代码默认 max_tokens=16384，按需消耗不会浪费。

# === Embedding 与 Reranker（外部 API；本项目统一走 LiteLLM proxy）===
EMBEDDING_PROVIDER=voyage         # voyage 单轨（2026-05-16 决议）；glm 代码 fallback 保留
VOYAGE_API_KEY=                    # 由 LiteLLM 注入，本项目代码不直接读
VOYAGE_EMBEDDING_MODEL=voyage-4-large   # 200M tokens 免费已加 payment，限速 3M TPM / 2000 RPM
VOYAGE_RERANK_MODEL=rerank-2.5          # 200M tokens 免费，单价 $0.05/M
GLM_EMBEDDING_MODEL=embedding-3          # 智谱代码 fallback，默认不主动使用

# Embedding 维度：1024（M3 决胜 2026-05-16，2048 collection 已 drop）
EMBEDDING_DIMENSIONS=1024
VOYAGE_OUTPUT_DIMENSION=1024      # 与 LiteLLM `config.yaml` 一致

# 全量索引可选启用 Voyage Batch API（标准 endpoint 33% 折扣，12h 完成窗口）
VOYAGE_USE_BATCH_API_FOR_FULL_INDEX=false

# === 索引并发（M2 §4.8）===
VOYAGE_TPM=3000000
VOYAGE_RPM=2000
MIMO_TPM=10000000
MIMO_RPM=100
INDEX_CONCURRENT_WORKERS=3
INDEX_VISION_CONCURRENT=8

# === Web 搜索 ===
TAVILY_API_KEY=

# === Qdrant ===
QDRANT_URL=http://host.docker.internal:6333
QDRANT_API_KEY=                   # 若启用
QDRANT_COLLECTION_PREFIX=tgpp_chunks

# === PostgreSQL（2026-05-27 解耦：tgpp 专属 postgres 容器）===
POSTGRES_PASSWORD=CHANGEME
DATABASE_URL=postgresql+asyncpg://tgpp_app:CHANGEME@tgpp-postgres:5432/tgpp_everything

# === Redis（2026-05-27 解耦：tgpp 专属 redis 容器，db=0）===
REDIS_PASSWORD=CHANGEME
REDIS_URL=redis://:CHANGEME@tgpp-redis:6379/0

# === Langfuse ===
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com

# === 成本告警（M7.4，仅 log warning）===
# 每日 / 每月美元阈值；超阈仅 log.warning，不接 webhook（Q2 2026-05-19 决策）
# 0 / 负值视作 disabled
ALERT_DAILY_USD=5.0
ALERT_DAILY_USD_CRITICAL=10.0
ALERT_MONTHLY_USD=50.0
# daily 聚合 job 跑的小时（本地时区 APP_TIMEZONE，默认 01:00）
ALERT_DAILY_AGGREGATE_HOUR=1
# 单进程 worker 部署可关掉 scheduler；多 worker 需在外层做 leader 选举
ALERT_SCHEDULER_ENABLED=true

# === 鉴权（多用户）===
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7
BOOTSTRAP_ADMIN_INVITE_CODE=       # 首次创建管理员时使用，创建后应清空或轮换
# CORS 允许的前端 origin。CSV / JSON list / 单 origin 三种写法都支持，
# 空字符串视作空 list（生产应填具体域名，绝不要写 *）。
# dev 期 Flutter web 默认从 :8080（flutter run -d chrome 默认）或 :8082（nginx）访问，
# 真机 / 局域网测试可加 http://<dev-ip>:8080。
ALLOWED_ORIGINS=http://localhost:8080,http://localhost:8082,http://127.0.0.1:8080

# === 用户每日对话配额 + Server酱推送（普通用户 role=user 限流；admin 豁免）===
# 每个普通用户每天最多对话次数（一次 POST /sessions/{sid}/messages 计 1 次），
# 按 APP_TIMEZONE 本地日 0 点切换；<=0 关闭（不限流、不通知）。
DAILY_CHAT_LIMIT=100
# Server酱³ 完整推送 URL（含 SendKey），形如 https://sctapi.ftqq.com/<SendKey>.send。
# 普通用户「当日首次对话 / 首次越界」各推一次；空 = 不推送（功能降级 no-op）。
# 可复用 infra/ingress/.env 的同一 SERVERCHAN_URL（与运维告警同一通道）。
SERVERCHAN_URL=

# === 文档摄取 ===
INGEST_DATA_DIR=/data/tgpp        # 原始 .doc + 中间 .docx + markdown
LIBREOFFICE_BIN=libreoffice       # 容器内路径

# === HuggingFace（GSMA/3GPP dataset 加载器使用）===
HF_TOKEN=                          # 可选；公开 dataset 匿名可读，带 token 能提升 anonymous 限流
```

**安全规则**：

- `.env` **永不入 git**（`.gitignore` 必须涵盖）
- `.env.example` 保留所有 key 名 + 注释 + 默认值，但敏感值留空
- 生产环境用 docker compose 的 `env_file` 注入

### 2.5 Docker Compose 框架

> 2026-05-27 update：dev / prod compose 均**自带** `postgres` + `redis`，对称结构；Qdrant + LiteLLM 仍复用宿主（attach 外部 network）。完整内容直接看 `deploy/docker-compose.yml`（dev）与 `deploy/docker-compose.prod.yml`（prod），本文不再镜像粘贴避免漂移。

关键 service 一览：

| service | dev 行为 | prod 行为 |
|---|---|---|
| `api` | 源码 bind-mount + `--reload`；端口 `8002:8002` 暴露宿主 | 镜像 COPY；仅 `expose: 8002`（由 ingress 直连） |
| `web` | 端口 `8082:80` 暴露宿主 | 仅 `expose: 80` |
| `ingest` | profile `ingest`，按需 `docker compose --profile ingest run --rm ingest ...` | 同 dev，profile 控制 |
| `postgres` | `tgpp-postgres`，`postgres:16-alpine`；`127.0.0.1:55432:5432` 暴露宿主回环 | `tgpp-postgres`，仅 `expose: 5432`（仅 compose 内可达） |
| `redis` | `tgpp-redis`，`redis:7-alpine`；`127.0.0.1:56379:6379` 暴露宿主回环 | `tgpp-redis`，仅 `expose: 6379` |

外部网络：

- `qdrant-net`（`name: p2-rag-assistant_default`）：让 api/ingest 用容器名访问宿主 Qdrant
- `litellm-net`（`name: litellm_default`）：同上访问 LiteLLM
- 已**移除** `dangdang-net` 引用（2026-05-27 解耦决议）

> Qdrant / LiteLLM 仍复用宿主；如未来要把它们也迁进项目，加 `deploy/docker-compose.standalone.yml` 即可。

### 2.6 Python 工程化

`backend/pyproject.toml`：

```toml
[project]
name = "tgpp-backend"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "pydantic>=2.7",
  "pydantic-settings>=2.4",
  "sqlalchemy>=2.0",
  "asyncpg>=0.29",
  "alembic>=1.13",
  "redis>=5.0",
  "qdrant-client>=1.11",
  "llama-index>=0.13",
  "llama-index-vector-stores-qdrant>=0.4",
  "llama-index-retrievers-bm25>=0.4",
  "llama-index-readers-docling>=0.3",
  "langchain>=0.3",
  "langchain-openai>=0.2",
  "langgraph>=0.2",
  "langgraph-checkpoint-postgres>=2.0",
  "voyageai>=0.3",
  "tavily-python>=0.5",
  "langfuse>=2.50",
  "python-jose[cryptography]>=3.3",
  "passlib[bcrypt]>=1.7",
  "httpx>=0.27",
  "structlog>=24.1",
  "tenacity>=9.0",
]

[project.optional-dependencies]
dev = [
  "ruff>=0.6",
  "black>=24.8",
  "mypy>=1.11",
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "pytest-cov>=5.0",
  "httpx>=0.27",
  "ragas>=0.2",
  "pyyaml>=6.0",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E","F","I","B","UP","SIM","RUF"]

[tool.black]
line-length = 100
target-version = ["py311"]

[tool.mypy]
python_version = "3.11"
strict_optional = true
warn_unused_ignores = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = ["unit", "integration", "eval"]
```

`ingestion/pyproject.toml` 类似，依赖：`docling`、`langchain`、`langchain-openai`、`qdrant-client`、`httpx`、`structlog`、`tenacity`、`typer`（CLI）。

包管理用 `uv`（更快、跨平台）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd backend && uv sync --dev
cd ../ingestion && uv sync --dev
```

### 2.7 Makefile 常用任务

`Makefile`：

```makefile
.PHONY: help dev lint test test-unit test-int eval down ingest-poc fmt

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-20s %s\n", $$1, $$2}'

dev:                      ## 启动后端 + 前端容器（dev 模式）
	docker compose -f deploy/docker-compose.yml up --build

down:                     ## 停掉并清理 dev 容器
	docker compose -f deploy/docker-compose.yml down

lint:                     ## ruff + black --check + mypy
	cd backend && uv run ruff check . && uv run black --check . && uv run mypy app
	cd ingestion && uv run ruff check . && uv run black --check .

fmt:                      ## ruff --fix + black
	cd backend && uv run ruff check --fix . && uv run black .
	cd ingestion && uv run ruff check --fix . && uv run black .

test-unit:                ## 跑后端单测
	cd backend && uv run pytest -m unit -q

test-int:                 ## 跑后端集成测（需起 ephemeral PG + Qdrant）
	cd backend && uv run pytest -m integration -q

test:                     ## 单测 + 集成测
	$(MAKE) test-unit
	$(MAKE) test-int

eval:                     ## RAG 评测（金标准集）
	cd backend && uv run pytest -m eval -q

ingest-poc:               ## 单文件解析 POC
	docker compose -f deploy/docker-compose.yml --profile ingest run --rm ingest \
		python -m ingestion.cli parse-single ${FILE}
```

### 2.8 `.gitignore`

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
.uv-cache/

# Flutter
**/.dart_tool/
**/.flutter-plugins
**/.flutter-plugins-dependencies
**/build/
**/.flutter-plugins
frontend/web/build/
frontend/android/.gradle/

# Env / 数据 / 日志
.env
.env.local
*.log
data/
eval-results/

# IDE
.vscode/
.idea/
.cursor/

# OS
.DS_Store
Thumbs.db
```

### 2.9 health check 占位

`backend/app/main.py` 先写最小骨架：

```python
from fastapi import FastAPI

app = FastAPI(title="3GPP-Everything API")

@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
```

`docker compose up` 后 `curl http://localhost:8002/health` 应返回 200。

## 3. 验收清单

> 标注：`[auto]` = Agent 自跑命令即可判定；`[human]` = 需要人介入（涉及账号/扩容/外部 secret/产品口径）。

- [ ] `[human]` `df -h` 显示 `/data` 可用空间 ≥ 50GB（扩容动作必须由人完成；< 50GB 时人须 approve "不进入全量索引"的偏离；M3 决胜 2026-05-16 后 2048 collection 已 drop，POC 期不再有双 collection 占用）
- [ ] `[auto]` `docker exec tgpp-postgres psql -U tgpp_app -d tgpp_everything -c '\dx'` 列出 `uuid-ossp`、`pgcrypto`
- [ ] `[auto]` `curl 127.0.0.1:6333/collections` 仍可访问、且本项目所有 collection 都以 `tgpp_chunks_` 开头
- [ ] `[auto]` `docker exec tgpp-redis redis-cli -a "$REDIS_PASSWORD" ping` 返回 PONG
- [ ] `[human]` `curl -s -H "Authorization: Bearer $LITELLM_API_KEY" http://127.0.0.1:4000/v1/models` 列出至少 `mimo-v2.5-pro`、`mimo-v2.5`、`embedding-3`、`voyage-4-large`、`rerank-2.5`（LiteLLM key 由人发放；旧 `voyage-3-large` / `rerank-2` 已下线，验收时不应出现）
- [ ] `[auto]` `make lint` 全绿
- [ ] `[auto]` `docker compose -f deploy/docker-compose.yml up --build` 起来后 `curl localhost:8002/health` 返回 200
- [ ] `[auto]` `docker compose down` 干净退出
- [ ] `[auto]` `.env.example` 与本文 §2.4 清单字段一一对应（CI 校验脚本可加）

## 4. 风险与排雷

| 风险 | 触发 | 应对 |
|------|------|------|
| `host.docker.internal` 在 Linux 默认不存在 | Linux 宿主 | 已用 `extra_hosts: host-gateway` 修复（仍保留以便 host 直跑路径） |
| `tgpp-postgres` 数据卷损坏 / 误删 | `docker volume rm`、磁盘故障 | `make prod-backup` 周期跑 pg_dump；走人审流程才能 `down -v`（CLAUDE.md §5.4） |
| LiteLLM 限流影响共享项目 | 高并发查询 | LiteLLM 单独 virtual key + 项目专属 rate limit |
| Qdrant 共享集群被对方损坏 | 别的项目误删 collection | tgpp 所有 collection 都 `tgpp_chunks_` 前缀；纵深防御：定期 snapshot |
| Docker volume 占满根盘 | 索引数据增长 | data-root 迁到 `/data` 后挂载新盘 |

## 5. 完成后下一步

- ✅ 本文档全部验收 → 进入 `02-ingestion-and-indexing.md`，开始 M1 单文件解析 POC

## 6. 变更记录

- 2026-05-27 — PG/Redis 从共享 `dangdang-postgres` / `dangdang-redis` 解耦为本项目专属 `tgpp-postgres` / `tgpp-redis`。Qdrant/LiteLLM 继续共享。详见 `docs/04-handoff/2026-05-27-decouple-from-dangdang.md`。原始"故意不在 compose 内起 PG/Redis 以节省 RAM"的决策由本次反转。
