.PHONY: help dev lint test test-unit test-int eval eval-daily eval-weekly down ingest-poc fmt \
        web-deps web-analyze web-test web-smoke web-smoke-chrome web-run web-build apk-build

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-20s %s\n", $$1, $$2}'

dev:                      ## 启动后端 + 前端容器（dev 模式）
	docker compose --env-file .env -f deploy/docker-compose.yml up --build

down:                     ## 停掉并清理 dev 容器
	docker compose --env-file .env -f deploy/docker-compose.yml down

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
# dev 默认指向本机后端；生产 nginx 同源反代时把它设成 /api/v1
API_BASE_URL ?= http://localhost:8002/api/v1

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

web-run:                  ## dev 起 Chrome 调试 (默认 8080 端口；确保后端 ALLOWED_ORIGINS 含此 origin)
	cd frontend && $(FLUTTER) run -d chrome --web-port=8080 \
		--dart-define=API_BASE_URL=$(API_BASE_URL)

web-build:                ## 产线 web 构建 → frontend/build/web (供 Dockerfile + nginx)
	cd frontend && $(FLUTTER) build web --release \
		--dart-define=API_BASE_URL=$(API_BASE_URL)

apk-build:                ## 真机 Android APK；在 Windows 上跑，按 docs 05 §13 用本机 IP 覆盖 API_BASE_URL
	cd frontend && $(FLUTTER) build apk --release \
		--dart-define=API_BASE_URL=$(API_BASE_URL)
