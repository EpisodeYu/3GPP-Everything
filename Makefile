.PHONY: help dev lint test test-unit test-int eval eval-daily eval-weekly down ingest-poc fmt \
        standalone-up standalone-down standalone-logs \
        web-deps web-analyze web-test web-smoke web-smoke-chrome web-run web-build web-docker apk-build \
        check-openapi-diff \
        prod-up prod-down prod-restart prod-logs prod-build prod-deploy prod-health \
        prod-backup prod-restore

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-20s %s\n", $$1, $$2}'

dev:                      ## 启动后端 + 前端容器（dev 模式）
	docker compose --env-file .env -f deploy/docker-compose.yml up --build

down:                     ## 停掉并清理 dev 容器
	docker compose --env-file .env -f deploy/docker-compose.yml down

# ----- standalone（零宿主依赖的单机自托管：自带 qdrant + litellm + pg + redis）-----
# 锚：deploy/docker-compose.standalone.yml + deploy/litellm/README.md
# 前置：cp .env.example .env；cp deploy/litellm/{config.yaml,.env}.example 去掉 .example 并填值。
STANDALONE_COMPOSE := docker compose --env-file .env -f deploy/docker-compose.standalone.yml

standalone-up:            ## 起自托管全栈（api+qdrant+litellm+pg+redis）；Web 另用 --profile web
	$(STANDALONE_COMPOSE) up --build -d
	@echo "✓ standalone 已起。探活: curl 127.0.0.1:8002/ready"
	@echo "  Web UI（可选）: make web-build && $(STANDALONE_COMPOSE) --profile web up -d web"

standalone-down:          ## 停掉 standalone 全栈（保留数据卷）
	$(STANDALONE_COMPOSE) down

standalone-logs:          ## 跟随 standalone 日志
	$(STANDALONE_COMPOSE) logs -f --tail=200

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

eval:                     ## RAG 评测（金标准集，含 smoke + daily + weekly；daily/weekly 需 RUN_LIVE_EVAL=1）
	cd backend && uv run pytest -m eval -q

eval-daily:               ## D13 daily 子集（hand_crafted ≥ 20 题，宽松档）；RUN_LIVE_EVAL=1 触发真 LLM
	cd backend && uv run pytest -m eval -q -k "daily or smoke"

eval-weekly:              ## D13 weekly 全集（≥ 140 题，M7 宽松/M8 严格）；RUN_LIVE_EVAL=1 触发真 LLM
	cd backend && uv run pytest -m eval -q -k "full or smoke"

ingest-poc:               ## 单文件解析 POC
	docker compose --env-file .env -f deploy/docker-compose.yml --profile ingest run --rm ingest \
		python -m ingestion.cli parse-single ${FILE}

# ----- 前端 (M5+) -----
# Flutter SDK 路径可覆盖：FLUTTER=/path/to/flutter make web-test
FLUTTER ?= /data/flutter/bin/flutter
# dev 默认指向本机后端；`make web-run` / `make web-build` 等命令用它
API_BASE_URL ?= http://localhost:8002/api/v1
# `make web-docker` 跑出来的镜像默认走同源相对路径，由 frontend/nginx/default.conf
# 把 /api/v1/ 反代到 tgpp-api:8002。原因见 frontend/nginx/default.conf 顶部注释：
# 浏览器从非 dev-box 机器访问 8082 时，硬嵌 localhost:8002 会触发 connect timeout。
# 想反向覆盖（如把 web 镜像跑在没反代的环境）：`make web-docker WEB_DOCKER_API_BASE_URL=http://<api-host>:8002/api/v1`
WEB_DOCKER_API_BASE_URL ?= /api/v1

web-deps:                 ## 前端依赖安装（pub get）
	cd frontend && $(FLUTTER) pub get

web-analyze:              ## 前端静态检查 (flutter analyze 0 警告即通过)
	cd frontend && $(FLUTTER) analyze

web-test:                 ## 前端 widget + unit 测试
	cd frontend && $(FLUTTER) test

web-smoke:                ## integration_test smoke 走 web-server (无需 chromedriver，Linux 服务器可跑)
	cd frontend && $(FLUTTER) drive \
		--driver=test_driver/integration_test.dart \
		--target=integration_test/login_flow_test.dart \
		-d web-server --browser-name=chrome --web-port=8088

web-smoke-chrome:         ## 真 Chrome (headed) + chromedriver smoke；Linux headless 易卡，推荐 macOS/Windows 上跑
	cd frontend && $(FLUTTER) drive \
		--driver=test_driver/integration_test.dart \
		--target=integration_test/login_flow_test.dart \
		-d chrome --browser-name=chrome --web-port=8088

web-smoke-chrome-headless: ## 等价 web-smoke-chrome，但用 CHROME_EXECUTABLE wrapper 强制 --headless=new；headless Linux server / CI 用
	CHROME_EXECUTABLE=$(CURDIR)/frontend/scripts/chrome-headless-wrapper.sh \
		$(MAKE) web-smoke-chrome

web-run:                  ## dev 起 Chrome 调试 (默认 8080 端口；确保后端 ALLOWED_ORIGINS 含此 origin)
	cd frontend && $(FLUTTER) run -d chrome --web-port=8080 \
		--dart-define=API_BASE_URL=$(API_BASE_URL)

web-build:                ## 产线 web 构建 → frontend/build/web (供 Dockerfile + nginx)
	cd frontend && $(FLUTTER) build web --release \
		--dart-define=API_BASE_URL=$(API_BASE_URL) \
		--dart-define=LANGFUSE_URL=$(LANGFUSE_URL)

# M5.6：先 build → 再 docker build。镜像本身只装 nginx + 静态产物 ~20MB，
# 不在镜像里装 Flutter SDK，理由见 frontend/Dockerfile 顶部注释。
LANGFUSE_URL ?= https://cloud.langfuse.com

web-docker:               ## 先 web-build 再 docker build -t tgpp-web frontend/（默认同源 /api/v1，nginx 反代到 api:8002）
	$(MAKE) web-build API_BASE_URL=$(WEB_DOCKER_API_BASE_URL)
	docker build -t tgpp-web frontend/

# M5.6 docs §14 [auto] 最后一条：把后端 /openapi.json 字段集与 Dart client
# fromJson 解析字段集 diff，schema 漂移 → exit 非零让 CI 失败。
# 必须在 backend 子目录跑 uv run，因 pyproject 在那里。
check-openapi-diff:       ## 比对后端 openapi.json 与前端 Dart client fromJson 字段集
	cd backend && uv run python ../scripts/check_openapi_diff.py

apk-build:                ## 真机 Android APK；在 Windows 上跑，按 docs 05 §13 用本机 IP 覆盖 API_BASE_URL
	cd frontend && $(FLUTTER) build apk --release \
		--dart-define=API_BASE_URL=$(API_BASE_URL) \
		--dart-define=LANGFUSE_URL=$(LANGFUSE_URL)

# ----- 生产部署 (M8) -----
# 锚：deploy/docker-compose.prod.yml + deploy/scripts/*.sh + docs/03-development/07-cicd-and-deployment.md
#      docs/04-handoff/2026-05-26-m8-deploy-bootstrap.md
#
# 架构：80/443 + TLS 卸载由 ~/infra/ingress/ 接管；本项目只起业务容器（api + web）。
#
# 前置：
#   1. .env 填好 ALLOWED_ORIGINS=...,https://3gpp-everything.org
#   2. Cloudflare DNS A 记录 @ → 公网 IP（灰云）
#   3. 服务器防火墙放行 80/443
#   4. ~/infra/ingress/ 项目就位 + .env 填好 + 跑过 init-letsencrypt.sh 签发证书
PROD_COMPOSE := docker compose --env-file .env -f deploy/docker-compose.prod.yml

prod-build:               ## 先 web-build 再 docker compose build api web（不起容器）
	$(MAKE) web-build API_BASE_URL=/api/v1
	$(PROD_COMPOSE) build api web

prod-up:                  ## 启动生产业务容器（api + web；nginx 入口由 ~/infra/ingress 管）
	$(PROD_COMPOSE) up -d
	@echo "✓ prod 业务容器已起（api + web）"
	@echo "  80/443 入口在 ~/infra/ingress：cd ~/infra/ingress && docker compose ps"
	@echo "  日志: make prod-logs"
	@echo "  探活: make prod-health"

prod-down:                ## 停止生产业务容器（不影响 ingress 与证书）
	$(PROD_COMPOSE) down

prod-restart:             ## 滚动重启 api + web
	$(PROD_COMPOSE) restart api web

prod-logs:                ## 跟随 prod 业务容器日志（Ctrl+C 退出）
	$(PROD_COMPOSE) logs -f --tail=200

prod-deploy:              ## 一键发布：web-build + docker build + up + 健康检查
	./deploy/scripts/deploy.sh

prod-health:              ## 探活：api /health /ready + 外网 https（含 ingress 链路）
	./deploy/scripts/healthcheck.sh

prod-backup:              ## 备份 PG dump + Qdrant snapshot 名 + BM25 + .env（证书在 ingress 项目）
	./deploy/scripts/backup.sh

prod-restore:             ## 从备份恢复（需 BACKUP=./backups/<ts>，例：make prod-restore BACKUP=./backups/20260526-180000）
	./deploy/scripts/restore.sh $(BACKUP)
