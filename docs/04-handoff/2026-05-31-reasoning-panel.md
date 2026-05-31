# 2026-05-31 · ReasoningPanel UX 优化（C+ 精简加强版）

> 用户在用户提问到首个 token 到达的几十秒里看不到任何"reasoning 文字"，体感像卡死。本次落一个"在回答位置出现的灰色折叠框，逐字刷新当前步骤的 LLM 输出"，类似 Claude / o1 的 reasoning 框，回答出来后自动折叠成 `已思考 X.Xs · N 步骤` 单行。

> 关联文档：[`docs/03-development/03-agent.md §7`](../03-development/03-agent.md) SSE 表 / [`docs/03-development/04-backend-api.md §4.2`](../03-development/04-backend-api.md) / [`docs/03-development/05-frontend.md §5.2`](../03-development/05-frontend.md)。属于 M5 后续 UX 迭代，不阻塞既有里程碑。

## 0. 设计取舍

实现前评估了三个方案：

| 方案 | 工作量 | 体验 |
|---|---|---|
| **A** 仅 summary 渲染（节点跑完一次性刷出人话）| ~3-4h | 快但中间几秒纯静态 |
| **B** 5 个 LLM 节点全部 `chat()` → `chat_stream()` 真流式 | ~15-17h | hyde 看着真有 reasoning 感；classify / multi_query / self_rag 流式 token 是 JSON 片段，体感像代码闪烁，**比一次性刷 summary 还差** |
| **C+** hyde 真流式 + 其它节点 summary 人话 + 中文节点名 | ~10-12h | hyde 那 5-10s 字符级刷新最有 reasoning 感（mimo `thinking=enabled`，输出 200-400 token 自然语言段落），其它短输出节点用 `node_end.summary` 一次性渲染人话 |

最终落 **C+**：用最低工程代价拿 95% 体验。

## 1. 后端改动

### 1.1 SSE 协议新增 `node_progress`（11 类事件）

[`backend/app/api/v1/chat.py`](../../backend/app/api/v1/chat.py)：

- 文件头注释从 10 类扩到 11 类，新增 `node_progress` 行 + 设计说明
- `stream()` 主循环加 `kind == "on_custom_event" and name == "node_progress"` 分支，透传 `{node, delta}`；兼容 LangGraph 不同版本对 custom event payload 的两种包装（嵌套 `data["data"]` 与扁平 `data`）

### 1.2 hyde 节点改字符流

[`backend/app/agent/nodes/hyde.py`](../../backend/app/agent/nodes/hyde.py) 完全重写：

- `chat()` → `chat_stream()` 真流式，每个 chunk 通过 `adispatch_custom_event("node_progress", {"node":"hyde","delta":...})` + `get_stream_writer()` 双通道 emit（与 retrieve_node 的 `chunks_hit` 同模式，让 `astream_events("v2")` / `astream(stream_mode="custom")` 都能拿到）
- 流式失败兜底：catch `LLMError` → 退到非流式 `chat()` → 整段一次性补一条 `node_progress`，UI 不至于空白（与 generate 节点 `_stream_answer` 同款 fallback）
- `max_tokens=8192` / `temperature=0.2` 沿用现状

### 1.3 `_summary_for_node_end` 重写为按节点分支

把"count"展开成"原文"，让前端 reasoning 框能直接拼"人话"：

| 节点 | 旧字段 | 新字段 |
|---|---|---|
| `classify` | `query_class` / `complexity` | + `rewritten_query`（取 `state.rewritten_queries[0]`）|
| `rewrite` | `rewritten_queries_count` | `rewritten_query: str`（原文）|
| `multi_query` | `rewritten_queries_count` | `sub_queries: list[str]`（前 5 条）|
| `retrieve` / `rerank` | `*_count` | 不变 |
| `self_rag` | `self_rag_verdict` / `retry_count` | + `confidence: float` |
| `hyde` | `hyde_doc` | 不变（hyde 已通过 progress event 累积，summary 仅做兜底）|

### 1.4 文档同步

- [`docs/03-development/03-agent.md §7`](../03-development/03-agent.md) SSE 事件表加 `node_progress` 行 + 设计说明
- [`docs/03-development/04-backend-api.md §4.2`](../03-development/04-backend-api.md) 加 reasoning 折叠框 + summary 字段调整说明

## 2. 前端改动

### 2.1 数据层

[`frontend/lib/data/api/messages_api.dart`](../../frontend/lib/data/api/messages_api.dart)：sealed `ChatEvent` 加 `NodeProgressEvent({node, delta})` + `fromFrame` 解析。

[`frontend/lib/features/chat/chat_controller.dart`](../../frontend/lib/features/chat/chat_controller.dart)：`ChatRunState` 加 4 个字段 + `copyWith` 用 sentinel 区分 "null = 显式清空" 与 "未传"：

```dart
final Map<String, String> reasoningByNode;  // hyde 字符级累积
final String? activeNode;                   // node_start 设、node_end 清
final DateTime? reasoningStartedAt;         // send 时设
final bool reasoningCollapsed;              // 首 token 自动 true
```

`_onEvent` 新增 `NodeProgressEvent` 累积逻辑 + 维护 `activeNode` + 首 token 自动折叠；`send` / `resume` 入口设置 `reasoningStartedAt`。

### 2.2 ReasoningPanel 组件（新）

[`frontend/lib/features/chat/widgets/reasoning_panel.dart`](../../frontend/lib/features/chat/widgets/reasoning_panel.dart)：

- 折叠态：单行 `已思考 X.Xs · N 步骤` + 上下箭头 + 点击切换
- 展开态：节点 chip 列（running 转圈 / done 打勾）+ 灰色滚动文字区（maxHeight 120）
  - hyde active：显示 `reasoningByNode['hyde']` 字符级累积，自动滚到底
  - 其它节点：i18n placeholder（active 时）/ summary helper 渲染的人话（done 时）
- 用户手动展开后用 `_userOverride` 标记，不被 controller 信号自动折叠覆盖
- `formatNodeSummary` 是 `@visibleForTesting` 纯函数，不依赖 BuildContext，易测

**关键决策**（CLAUDE.md §4.3 自主决策）：

1. **去掉 `Timer.periodic` 秒数刷新** —— 真路径下 token 流持续到达让 ChatPage 频繁 rebuild，ReasoningPanel 跟着 rebuild，秒数自然刷新；同时让 widget test `pumpAndSettle` 不会被 ticker 卡死
2. **去掉 `AnimatedSize` 折叠动画** —— 与运行节点的 `CircularProgressIndicator`（无限动画）叠加在 widget test 下让 layout 不稳定；视觉差极小（180ms），测试稳定性收益明显
3. **删除独立 `NodeStatusStrip`**（不保留双 UI）—— 信息重复，reasoning panel 完全覆盖原功能；现有 widget test 一并迁移到 `reasoning_panel_test.dart`

### 2.3 集成 + 视觉收尾

[`frontend/lib/features/chat/chat_page.dart`](../../frontend/lib/features/chat/chat_page.dart)：

- 删除外层独立 `NodeStatusStrip`（在 Composer 上方一行）
- 在 `_MessagesList` 内 streaming bubble **上方**插入 `ReasoningPanel` —— 视觉上"在回答的位置"，与用户预期对齐

[`frontend/lib/features/chat/widgets/message_bubble.dart`](../../frontend/lib/features/chat/widgets/message_bubble.dart)：`StreamingAssistantBubble` 在 `partial.isEmpty` 时不再显示 typing dots（交给 reasoning 框），避免双 typing 视觉重复。

### 2.4 i18n

[`frontend/lib/core/l10n/app_zh.arb`](../../frontend/lib/core/l10n/app_zh.arb) + [`app_en.arb`](../../frontend/lib/core/l10n/app_en.arb) 各 18 条新 keys：

- 9 个节点名：`reasoningClassify` / `reasoningRewrite` / `reasoningHyde` / `reasoningMultiQuery` / `reasoningRetrieve` / `reasoningRerank` / `reasoningGenerate` / `reasoningSelfRag` / `reasoningToolDispatch`
- 折叠/展开：`reasoningCollapsedTitle({seconds, steps})` / `reasoningExpand` / `reasoningCollapse` / `reasoningWaiting`
- 6 个 done 文案：`reasoningClassifyDone` / `reasoningRewriteDone` / `reasoningMultiQueryDone({count})` / `reasoningRetrieveDone({count})` / `reasoningRerankDone({count})` / `reasoningSelfRagDone({verdict, confidence})`

## 3. 测试

| 套 | 状态 | 备注 |
|---|---|---|
| `make lint`（ruff + mypy） | 全绿 | |
| backend unit (`tests/unit/`) | **356/356 passed** | 含新 6 条 hyde_node 用例：流式正常 / 流式失败回 chat() / 全失败返 None / max_tokens 守约 / `node_progress` adispatch 调用断言 |
| backend API integration (`tests/integration/api/`) | **99/99 passed** | 含新 3 条 chat SSE 用例：`node_progress` 透传 / 嵌套 `data["data"]` 兼容 / `node_end.summary` 字段断言 |
| backend agent integration (`tests/integration/agent/`) | 全绿 | 真实环境 LLM/Qdrant；含 5 题 complex QA 端到端跑 hyde 流式路径（首次跑挂连接抖动，重跑过） |
| `flutter analyze` | 0 issues | |
| `flutter test`（含 widget + golden + integration） | **216/216 passed** | 从 M5.6 的 168 测扩展 +48；新增 5 条 chat_controller reasoning 状态机 + 6 条 reasoning_panel widget 测 |

## 4. 体验

用户发问 → 立即在消息列表底部出现灰色折叠框，展开态：

1. 顶部节点 chip 列（running 转圈 / done 打勾）
2. 底部灰色文字区：
   - **hyde 节点**：字符级流式刷新（LLM 在写"假设答案章节文本"时一字字出现）
   - 其它 LLM 节点：跑完瞬间刷出 summary 人话（`改写为: ...` / `拆出 N 个子查询: ...` / `自检: accept · 置信度 0.87`）
   - retrieve / rerank：`找到 N 个候选` / `Top-N 排序完成`

首个 `token` 到达 → 自动折叠成单行 `已思考 12.3s · 6 步骤` + 上下箭头；用户可手动展开/折叠，折叠后点开仍能看到完整 hyde 流式内容（复盘）。

## 5. 留给人审

- **`[human]` 真机视觉**：dark / light 主题下灰色文字对比度，手机窄屏下 chip 列横向滚动手感（M5.6 i18n + 主题切换 + golden 已有基础设施，本次未补 reasoning_panel 的 golden —— 视觉相对简单，先跑真机回归再视情况补）
- **`[human]` 性能**：hyde 真流式让前端每秒收到 ~30-50 个 SSE 帧；reasoning panel 灰色文字区在 2k 字符内未做 virtualization。如真路径反馈卡，加 `LimitedBox` + 截断 head/tail
- **可选扩展**：mimo `thinking=enabled` 的 hyde reasoning_content 也想流式给用户看，需要扩 `node_progress` payload（加 `kind: 'reasoning' | 'content'`）。当前实现只 forward `content` chunk

## 6. 剩余风险

- 用户手动展开后的 reasoning 框在长 hyde_doc 下可能视觉拥挤（maxHeight=120 + scroll；实际 mimo hyde 输出 ~200-400 token ≈ 1k-2k 字符，120 高 + scroll 下基本顺畅）
- `_summary_for_node_end` 字段名与前端 `formatNodeSummary` 严格耦合：后端把 `candidates_count` 改成 `candidate_count`，前端 reasoning 文本会失活降级到节点名 prefix（不会崩，但 UX 退化）；已有 `test_node_end_summary_carries_human_readable_fields` 集成测把字段名锁死

## 7. 文件清单

**后端（4 modified）**

- `backend/app/api/v1/chat.py` — SSE 透传 + summary 字段重写
- `backend/app/agent/nodes/hyde.py` — chat_stream + node_progress emit + 兜底
- `backend/tests/unit/agent/test_hyde_node.py` — 5→6 条用例（覆盖流式 / 兜底 / 全失败 / max_tokens / progress emit）
- `backend/tests/integration/api/test_chat.py` — +3 条用例（progress 透传 / 嵌套兼容 / summary 字段）

**前端（4 new + 7 modified + 2 deleted）**

新：
- `frontend/lib/features/chat/widgets/reasoning_panel.dart`
- `frontend/test/features/chat/widgets/reasoning_panel_test.dart`

modified：
- `frontend/lib/data/api/messages_api.dart`（NodeProgressEvent 类 + fromFrame 分支）
- `frontend/lib/features/chat/chat_controller.dart`（4 字段 + sentinel copyWith + _onEvent）
- `frontend/lib/features/chat/chat_page.dart`（删独立 NodeStatusStrip + 嵌入 ReasoningPanel）
- `frontend/lib/features/chat/widgets/message_bubble.dart`（partial 空时不渲染 typing dots）
- `frontend/lib/core/l10n/app_zh.arb` + `app_en.arb`（18 条 keys）+ codegen 产物
- `frontend/test/features/chat/chat_controller_test.dart`（+5 条 reasoning 用例）

deleted：
- `frontend/lib/features/chat/widgets/node_status_strip.dart`
- `frontend/test/features/chat/widgets/node_status_strip_test.dart`

**文档（3 modified + 1 new）**

- `docs/03-development/03-agent.md`（§7 SSE 表 +1 行）
- `docs/03-development/04-backend-api.md`（§4.2 +reasoning 段）
- `docs/03-development/05-frontend.md`（§5.2 重写 + 文件树注释 + ChatRunState 字段表 + _onEvent switch）
- 本文件
