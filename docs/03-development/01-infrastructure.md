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
- 验收：`df -h` 显示 `/data`（或承载 Docker/Qdrant/项目数据的挂载点）可用 ≥ 50GB；< 50GB 时 M2-M3 ablation 期必须及早 drop 输者维度 collection（不允许 2048+1024 长期共存）

### 2.2 共享服务专属命名空间

**PostgreSQL**（已运行 :5432）：

```sql
CREATE USER tgpp_app WITH PASSWORD '...';
CREATE DATABASE tgpp_everything OWNER tgpp_app;
GRANT ALL PRIVILEGES ON DATABASE tgpp_everything TO tgpp_app;
-- 启用扩展（在 tgpp_everything 内）
\c tgpp_everything
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
-- 如未来需 pgvector fallback:
-- CREATE EXTENSION IF NOT EXISTS "vector";
```

**Qdrant**（已运行 :6333）：

- 无 db 概念，用 collection 命名隔离
- 启动期不需要预创建，由 `ingestion.indexer` 在首次写入时创建
- 检查 Qdrant 是否启用了 API key：`curl -s 127.0.0.1:6333/`；如启用则在 `.env` 配置

**Redis**（已运行 :6379）：

- 选用 `db=5`（确认不冲突：`redis-cli CLIENT LIST | grep "db=5"`）
- 无需建库，连接时指定 `?db=5`

**LiteLLM**（已运行 :4000）：

- 已有 `LITELLM_MASTER_KEY` —— 项目复用同 key，或在 LiteLLM 配置内新建一个 virtual key 限制只能访问本项目用到的 model
- 项目内访问地址：`http://127.0.0.1:4000/v1`（或 `http://host.docker.internal:4000/v1`）

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

# M2-M3 维度 ablation：MRL truncate+renorm 一次 API 调用同时产 2048/1024 两 collection
EMBEDDING_DIMENSIONS=2048,1024
VOYAGE_OUTPUT_DIMENSION=2048      # LiteLLM `config.yaml` 也已显式声明

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

# === PostgreSQL ===
DATABASE_URL=postgresql+asyncpg://tgpp_app:CHANGEME@host.docker.internal:5432/tgpp_everything

# === Redis ===
REDIS_URL=redis://host.docker.internal:6379/5

# === Langfuse ===
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com

# === 鉴权（多用户）===
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7
BOOTSTRAP_ADMIN_INVITE_CODE=       # 首次创建管理员时使用，创建后应清空或轮换
ALLOWED_ORIGINS=https://tgpp.example.com

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

`deploy/docker-compose.yml`（dev）：

```yaml
name: tgpp

services:
  api:
    build:
      context: ../backend
      dockerfile: Dockerfile
    container_name: tgpp-api
    env_file: ../.env
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "8002:8002"
    volumes:
      - ../backend:/app                # dev 热重载
      - ${INGEST_DATA_DIR:-./data}:/data/tgpp
    networks:
      - tgpp-net
    restart: unless-stopped

  ingest:
    build:
      context: ../ingestion
      dockerfile: Dockerfile
    container_name: tgpp-ingest
    env_file: ../.env
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ../ingestion:/app
      - ${INGEST_DATA_DIR:-./data}:/data/tgpp
    networks:
      - tgpp-net
    profiles: ["ingest"]               # 默认不启，按需 docker compose --profile ingest run --rm ingest ...

  web:
    build:
      context: ../frontend
      dockerfile: Dockerfile
    container_name: tgpp-web
    ports:
      - "8082:80"
    networks:
      - tgpp-net
    depends_on: [api]
    restart: unless-stopped

networks:
  tgpp-net:
    driver: bridge
```

> 故意**不在本 compose 内**起 Qdrant / PG / Redis / LiteLLM —— 复用宿主机已有实例。如未来要迁移到独立容器，加 `deploy/docker-compose.standalone.yml` 即可。

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

- [ ] `[human]` `df -h` 显示 `/data` 可用空间 ≥ 50GB（扩容动作必须由人完成；< 50GB 时人须 approve "不进入全量索引 / M2-M3 ablation 期必须及早 drop 输者维度 collection"的偏离）
- [ ] `[auto]` `psql -h 127.0.0.1 -U tgpp_app -d tgpp_everything -c '\dx'` 列出 `uuid-ossp`、`pgcrypto`
- [ ] `[auto]` `curl 127.0.0.1:6333/collections` 仍可访问、且本项目所有 collection 都以 `tgpp_chunks_` 开头
- [ ] `[auto]` `redis-cli -n 5 ping` 返回 PONG
- [ ] `[human]` `curl -s -H "Authorization: Bearer $LITELLM_API_KEY" http://127.0.0.1:4000/v1/models` 列出至少 `mimo-v2.5-pro`、`mimo-v2.5`、`embedding-3`、`voyage-4-large`、`rerank-2.5`（LiteLLM key 由人发放；旧 `voyage-3-large` / `rerank-2` 已下线，验收时不应出现）
- [ ] `[auto]` `make lint` 全绿
- [ ] `[auto]` `docker compose -f deploy/docker-compose.yml up --build` 起来后 `curl localhost:8002/health` 返回 200
- [ ] `[auto]` `docker compose down` 干净退出
- [ ] `[auto]` `.env.example` 与本文 §2.4 清单字段一一对应（CI 校验脚本可加）

## 4. 风险与排雷

| 风险 | 触发 | 应对 |
|------|------|------|
| `host.docker.internal` 在 Linux 默认不存在 | Linux 宿主 | 已用 `extra_hosts: host-gateway` 修复 |
| 共享 Postgres 用户权限被覆盖 | DBA 改了授权 | 项目独立 db owner = `tgpp_app`，避免改其他库 |
| Redis db=5 已被占用 | 共享实例 | 优先选未用的 db number；可写一个 `scripts/check-redis-db.sh` 启动前自检 |
| LiteLLM 限流影响共享项目 | 高并发查询 | LiteLLM 单独 virtual key + 项目专属 rate limit |
| Docker volume 占满根盘 | 索引数据增长 | data-root 迁到 `/data` 后挂载新盘 |

## 5. 完成后下一步

- ✅ 本文档全部验收 → 进入 `02-ingestion-and-indexing.md`，开始 M1 单文件解析 POC
