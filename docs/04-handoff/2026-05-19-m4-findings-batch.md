# M4 端到端人审 F-1 ~ F-6 修复批

**日期**：2026-05-19
**范围**：`docs/04-handoff/2026-05-18-m4-complete.md §5.3` 列出的 6 个 finding 一次性修复
**触发**：人指令 "模拟真实步骤调查并修复所有已知问题"
**协议**：`CLAUDE.md` vibe coding §1-§9 + `docs/00-vibe-coding-protocol.md` §4

---

## 1. 修复清单

| # | 严重度 | 修复 | 主要改动 |
|---|---|---|---|
| **F-1** | 🟠 高 | DELETE /runs/{rid} 真的能停 SSE | `main.py` lifespan 接 `AsyncPostgresSaver`；`chat.py` 加 `_iter_with_cancel` race loop + `app.state.in_flight_cancels` registry；`cancel_run` 双通道（set event + aupdate_state）|
| **F-2** | 🟠 高 | `/docs/{spec_id}` PG 不再 500 | `docs.py::get_doc` 不再 SQL GROUP BY JSON 列，改 Python 端 dict 聚合（单 spec ~2-3k 行 fetch < 100ms）|
| **F-3** | 🟡 中 | SSE 出齐 `chunks_hit` | `retrieve.py` cache-hit 分支补 `_emit_chunks_hit` 调用 |
| **F-4** | 🟡 中 | `/chunks/{cid}` content 真返回 | `docs.py` 加 `_fetch_content_map`，从 Qdrant payload 拉 content（lazy `app.state.qdrant_client`，conftest 关掉 → 测试走 raw_extra fallback）|
| **F-5** | 🟡 中 | `/sessions/{sid}/messages/{mid}` 注册 | 新增 `api/v1/messages.py` + `schemas/messages.py`；含 citations 派生 |
| **F-6** | 🟢 低 | sessions/{sid} 与 messages 拆分 | 新增 `GET /sessions/{sid}/messages` 分页列表；`SessionOut` 保持原样不内嵌（避免单大 response，符合人审 2026-05-19 选项 A）|

## 2. 设计决策

### 2.1 F-1：双通道 cancel

- **通道 A（主）**：`app.state.in_flight_cancels: dict[run_id, asyncio.Event]`，`send_message` / `resume_session` 注册；DELETE set event；SSE 流用 `asyncio.wait(FIRST_COMPLETED)` race 抢断正在 await 的 `astream_events.__anext__()`，把 CancelledError 注入 LiteLLM streaming
- **通道 B（持久化）**：`aupdate_state({"cancelled": True})`，写到 PostgresSaver checkpoint，让后续 resume / debug 可见

两通道幂等。M4.8 的 best-effort 路径（只有通道 B）在没接 checkpointer 时实际 no-op，是 2026-05-19 端到端人审 F-1 暴露的根因；本次同时接通了 checkpointer 与新增 race 机制。

cancel race 集成测：`test_chat.py::test_delete_cancels_inflight_sse_stream_via_race` 用 hang graph 模拟 LLM 卡住，验证 DELETE → cancelled event + status='cancelled' + registry 清空 + aupdate_state 也被调过。

### 2.2 F-2：放弃 SQL GROUP BY JSON

PG 的 JSON / JSONB 列没等值算子，`GROUP BY section_path` 直接 `UndefinedFunctionError`。三个备选：
- (a) `array_to_string(jsonb_array_elements_text(section_path))` cast — SQL 复杂、SQLite 不兼容、需要写 dialect 分支
- (b) `section_path::text` cast — PG-only，SQLite 测试断
- (c) **Python 端聚合**（本次选择）— 单 spec ~2-3k 行 × 3 个短列 fetch < 100ms，端到端 ≈ 章节树渲染本身的时延，可接受

### 2.3 F-4：Qdrant 优先 + raw_extra fallback

`chunks_meta` 表无 `content` 列（ingestion 现状只把 content 写 Qdrant payload）。两选项：
- (a) **Qdrant 拉**（本次选择）— `app.state.qdrant_client` lazy 单例，`/chunks/{cid}` + `/docs/{spec_id}/sections/...` 共用 `_fetch_content_map`。集成测路径 conftest 设 `qdrant_client_disabled = True` → 退回 raw_extra
- (b) schema 删 content 字段 — 改动小但前端要两次请求（先 chunk metadata 再 section 全文 + char_offset 高亮），用户体验差

人审 2026-05-19 选 (a)：单 chunk 详情视图（引用 chip → 气泡显示原文）是 Reader 关键闭环，content 必须直接返回。

### 2.4 F-5 / F-6：拆分列表 + 详情

§2 路由总表原写 `GET /sessions/{sid}` 返"会话元信息 + 消息列表"。本次拆分：
- `GET /sessions/{sid}` 仍只返 `SessionOut`（不变）
- `GET /sessions/{sid}/messages` 分页列消息 + citations（新）
- `GET /sessions/{sid}/messages/{mid}` 单条详情 + citations（新）

理由：长会话内嵌 messages → 单 response 体积爆炸；前端分页加载更自然。`§2` 表已同步更新口径。

## 3. 兼容性 / 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 多 worker 部署下 `app.state.in_flight_cancels` 不跨 worker | 集群部署时 DELETE 可能命不中 | M4 dev 单进程不受影响；M8 上线前若上 worker，cancel 改走 Redis pub/sub |
| Qdrant 不可达时 `/chunks/{cid}` content 退回 raw_extra（PG 上 raw_extra 没 content）→ 返回空 content | Reader 看不到正文 | 与 F-4 修前一样；附带 log warn。M8 前可加 Sentry 告警 |
| `lifespan` 尝试连 PG / Qdrant / LiteLLM 起 worker；任一不可达 → 单例 fallback | 起动稍慢但不阻塞 | lifespan 全 try/except suppress；conftest 设 `disable_agent_init=True` 跳过 |
| AsyncPostgresSaver 首次起 worker 时 `saver.setup()` 会建表 | PG 多了 `checkpoint*` 系列表 | 与 LangGraph 文档一致；备份策略 `00-overview.md §2` 已涵盖 |

## 4. 文档同步

| 文档 | 改动 |
|---|---|
| `docs/03-development/04-backend-api.md` §2 | sessions/{sid} 注明不含 messages；新增 /messages 列表 + 单条路由两行；DELETE /runs/{rid} 说明 race 机制 |
| `docs/04-handoff/2026-05-19-m4-findings-batch.md` | 本文（新增）|
| `backend/pyproject.toml` dev | 补 `aiosqlite>=0.20`（集成测必需，M4.10 后 transitive drop 后丢失）|

## 5. 自测结果

```
$ make lint
backend ruff/black/mypy + ingestion ruff/black: All checks passed

$ uv run pytest -m unit -q (backend)
176 passed

$ uv run pytest -m integration tests/integration/api -q (backend, before F-1/F-5 测试)
80 passed → 80 + 5 (test_messages.py) + 1 (test_chat::test_delete_cancels_inflight_sse_stream_via_race)
= 86 passed

$ uv run pytest -q (ingestion)
292 passed, 6 skipped（1 flaky test_indexer_concurrent 单跑过；与 backend 改动无关）
```

集成测新增 6 case：
- `test_messages.py`：5 case（list/get/404/RBAC/auth）
- `test_chat.py::test_delete_cancels_inflight_sse_stream_via_race`：1 case（F-1 cancel race 端到端）

## 6. 关联文档

- 上游 finding 登记：`docs/04-handoff/2026-05-18-m4-complete.md §5.3`
- API 路由表：`docs/03-development/04-backend-api.md §2`
- M4.8 cancel best-effort 历史：`docs/04-handoff/2026-05-18-m4.8-completion.md §5`
- 后续：M5 Flutter 可以基于稳定的 cancel + messages 路由开工；`docs/03-development/05-frontend.md`
