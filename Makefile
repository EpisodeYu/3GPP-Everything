.PHONY: help dev lint test test-unit test-int eval eval-daily eval-weekly down ingest-poc fmt

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
