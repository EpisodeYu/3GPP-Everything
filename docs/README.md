# 3GPP-Everything 项目文档索引

整个 Plan 分为三部分，按顺序阅读：

## 第 1 部分 - 需求澄清

- [`01-requirements.md`](./01-requirements.md) — 项目定位、用户场景、功能/非功能需求、验收标准、本期不做清单

## 第 2 部分 - 技术选型

- [`02-tech-selection.md`](./02-tech-selection.md) — 选型总表、LLM/Embedding/Reranker/Vision 决策、POC 计划、成本估算、替换路径

## 第 3 部分 - 详细开发规划（多份）

按依赖顺序：

| # | 文档 | 主题 |
|---|------|------|
| 00 | [`03-development/00-overview.md`](./03-development/00-overview.md) | 总览、里程碑 Gantt、目录骨架、命名约定 |
| 01 | [`03-development/01-infrastructure.md`](./03-development/01-infrastructure.md) | 磁盘扩容、共享服务命名空间、`.env`、Docker Compose 雏形、Makefile |
| 02 | [`03-development/02-ingestion-and-indexing.md`](./03-development/02-ingestion-and-indexing.md) | FTP 爬虫、LibreOffice + Docling 解析、Vision 描述、chunking、Qdrant + BM25 索引 |
| 03 | [`03-development/03-agent.md`](./03-development/03-agent.md) | LangGraph 状态图、节点、self-RAG、工具节点、PG checkpointer、流式协议 |
| 04 | [`03-development/04-backend-api.md`](./03-development/04-backend-api.md) | FastAPI 路由表、PG schema、SSE 事件、鉴权、Alembic、限流 |
| 05 | [`03-development/05-frontend.md`](./03-development/05-frontend.md) | Flutter Web+Android、Riverpod、SSE 客户端、聊天/阅读器/管理页 |
| 06 | [`03-development/06-evaluation-and-observability.md`](./03-development/06-evaluation-and-observability.md) | 金标准集、Ragas、Langfuse、Embedding POC 决胜评测、成本告警 |
| 07 | [`03-development/07-cicd-and-deployment.md`](./03-development/07-cicd-and-deployment.md) | GitHub Actions、生产 Compose、Nginx + Let's Encrypt、备份/回滚、Runbook |

## 阅读顺序建议

```
01 → 02 → 03-dev/00 → 01 → 02 → 03 → 04 → 05 → 06 → 07
```

实施期允许局部并行：

- `02 摄取` 与 `04 后端骨架` 可并行（仅依赖 `01 基础设施`）
- `03 Agent` 必须等 `02 索引可用`
- `05 前端` 必须等 `04 后端 API 契约稳定`
- `06 评测` 可与 `04/05` 并行
