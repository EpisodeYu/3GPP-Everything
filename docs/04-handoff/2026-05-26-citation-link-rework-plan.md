# 2026-05-26 · Citation 超链接化 + 移除 RawLookup 模式 · 计划

> Plan-only。已与 user 对齐到 §3 决策点；待 user approve 后再开工。

## 一、背景 · 用户痛点

1. **Composer 输入框失效**：QA 模式下点 RawLookup ChoiceChip 后有概率"光标闪烁但按键无效"。  
   → 已在本次会话修复（commit pending）；根因：`Focus(onKeyEvent) + TextField(focusNode)` 双层 FocusNode 在 Flutter Web 下偶发 `<textarea>` attach/detach 竞态。
2. **协议章节引用不可点击**：聊天回答里出现的 `[38.213 §8.1]` 看起来跟普通文本一样、没主色、也没点击行为。
3. **RawLookup 模式可被删**：用户希望产品只保留 QA 模式 + 在回答里把协议章节做成可跳转的超链接。

---

## 二、Citation 渲染 Bug · 根因分析（RCA）

**现状（坏的）**：
- 后端 generate 节点的 LLM 实际输出：`[38.213 §8.1]`（仅 spec + section）
- 前端 `CitationInlineSyntax` 正则：`r'\[(\d+\.\d+) §([\d\.]+) ¶(\d+)\]'`  
  → 强制要求 `¶<rank>` 三段全在才匹配
- 不匹配 → markdown 把 `[38.213 §8.1]` 当普通文本 → 无 chip、无 link、无点击

**证据**：本次会话 curl 跑通的 PRACH 回答 final event，每处引用都没有 `¶`。

**为什么以前没被发现**：
- `composer_test` / `chat_controller_test` 都 mock 死了 SSE event，没断言 markdown 渲染层
- M5.3 交付报告里只断言了 "正则单元测试 + chip widget 测试" 全过，没跑端到端真 LLM

---

## 三、修复方案

### Step 1 · 修 Citation 链接（核心）

**前端**：
- `frontend/lib/features/chat/widgets/citation_chip.dart`
  - `CitationInlineSyntax._pattern` 把 `¶<rank>` 改为**可选**：`r'\[(\d+\.\d+) §([\d\.]+)(?: ¶(\d+))?\]'`
  - rank 缺失时取 `0`（哨兵值）；`citationsByRank` lookup 拿不到对应 chunk 时降级为"只跳 spec+section，不弹 chunk sheet"
- `CitationChip` 视觉 + 行为按 user 选项调整：
  - **决策点 A · 视觉**：等 user 在交互问卷里钉死（A1 chip / A2 蓝下划线 link / A3 hover 才变色），先按 user 当前回答理解为"chip 形态保留但要让主色更明显 + 鼠标可点"，落地默认 = A1+保持主色 + 让 click 真生效（A1 本就已是主色，是 bug 导致不渲染）
  - **决策点 B · 点击行为**：user 已选 **B3** → 单击直接跳 reader `/reader/{spec}/{section}`（不再弹 sheet），hover 时弹 tooltip 预览 chunk
  - **决策点 C · 长按行为**：保持现状（复制原始引用文本），user 未明确选择 → 默认 C1
- Tooltip 预览实现：用 Flutter `Tooltip` widget 触发 chunk 预览，里面挂一个 `FutureBuilder` 拉 `GET /chunks/{chunk_id}`，缺 chunkId 时显示 "spec §section（无 chunk 上下文）"

**后端（可选 - 优化精度）**：
- `app/agent/nodes/generate.py` 的 prompt 改成要求 LLM 输出 `[spec §section ¶rank]`，在 context build 时把每个 chunk 标注 `[rank=N]` 喂给 LLM
- ⚠ 这条**非必须**：前端正则放宽后旧格式 `[spec §section]` 已能复活；带 ¶ 后能多一层精确定位 chunk 锚点
- 取舍：等 Step 1 前端落地后看实际效果再决定要不要追加；如果跳到 section 就够了，¶rank 完全不必加

**测试**：
- 单元测试：`citation_chip_test.dart` 加 case：正则匹配 `[38.213 §8.1]`（无 ¶）和 `[38.213 §8.1 ¶3]`（有 ¶）都生成 chip
- Widget 测试：chip 点击触发 `GoRouter.go('/reader/...')`，不再先弹 sheet
- 集成测试（手动）：发问 → final → 鼠标 hover chip 看 tooltip → 点击跳 reader

**预估**：~1.5h（前端 + 测试 + Tooltip 设计）

---

### Step 2 · 删 RawLookup 模式

**后端**：
- `app/schemas/chat.py` / `sessions.py` / `agent/state.py`：`Mode = Literal["qa", "raw_lookup"]` → `Literal["qa"]`
- `app/agent/graph.py`：
  - 删 `_entry_router` 的 raw_lookup 分支
  - 删 `_after_rerank` 的 end 分支（统一走 generate）
  - 简化 `build_graph` 的 conditional edges：去掉 `"raw_lookup": "retrieve"` 这条
- `app/db/models.py`：`mode_default` 列**保留不动**（避免 schema migration；默认 'qa' 即可，老数据里 'raw_lookup' 后续读出来就当 'qa' 处理）
- 测试清理：
  - 删 `backend/tests/integration/agent/test_raw_lookup.py`
  - `tests/integration/api/test_chat.py` 等里 `mode="raw_lookup"` 的 case 改 'qa' 或删
  - `backend/tests/integration/api/test_sessions.py` 等里 `mode_default='raw_lookup'` 改 'qa'

**前端**：
- `Composer`：删 ChoiceChip toggle、`mode` / `onModeChanged` 参数
- `chat_page.dart`：删 `_mode` state、send 时不再传 mode
- `sessions_api.dart`：`Mode` enum/literal 窄化为只有 'qa'
- 测试清理：composer_test 删 mode toggle 用例

**数据库**：dev DB 不迁移；M8 上线前如需清理（统一 mode='qa'）再加 alembic migration

**文档**：
- `docs/03-development/05-frontend.md §5.5`：删 mode toggle 描述
- `docs/03-development/03-agent.md`：删 raw_lookup 分支说明
- `docs/03-development/04-backend-api.md`：sessions schema 更新
- `README.md`：mode 相关行删

**预估**：~2h

---

### Step 3 · 自测 + 部署（~1h）

- `flutter analyze` + `flutter test test/features/chat/`（确认 +28 -39 → +28 -39 没有新回归）
- 后端 `pytest tests/unit/agent tests/integration/agent` + `tests/integration/api`
- `make web-build` → 重建 tgpp-web 镜像 → 重启
- 端到端：浏览器实测 send → final → citation 可点 → tooltip 出来 → 跳 reader

---

## 四、推荐执行顺序

**先 Step 1（修 citation 链接），再 Step 2（删 RawLookup）**。理由：

1. **Step 1 是 broken 功能修复**，用户期待已久；Step 2 只是清理冗余 UX，不阻塞核心价值
2. **Step 1 工作量小（~1.5h），快速验收**；Step 2 涉及前后端 + 文档 + 测试改动面大（~2h），单独一次 PR 评审更清晰
3. **Step 1 + Step 2 可分两个 commit**（feat: citation link; chore: remove raw_lookup），同一个 PR 也可分开；按 vibe coding §7 Conventional Commits

**关于先修 composer 输入 bug 还是先做本计划**：
- composer 输入 bug 修复**已落库**（uncommitted），是阻塞性 bug，建议**先单独 commit + 部署一次**，验证 user 反馈"修好了"
- 然后再开始 Step 1 + Step 2

```
合理顺序：
1. commit composer 焦点 bug 修复（已改完，待 commit）→ 已部署 → user 已验收为"修好了"
2. Step 1 citation link 修复（含放宽正则 + B3 hover tooltip + 单击直跳）
3. Step 2 删 RawLookup 双模式
4. 一并部署 + 让 user 验收
```

---

## 五、需要 user 决策的剩余事项

| 决策点 | 默认建议 | 备注 |
|---|---|---|
| Citation 视觉 (A1/A2/A3) | A1（保留主色 chip 形态，把 bug 修了让它显示出来） | user 提到"既不是主色也不可点击"是因为 bug，不是不喜欢 chip 形态。如果想看实际效果再决定要不要换 link，建议先 A1 落地 |
| Citation 长按 (C1/C2/C3) | C1（保留复制） | 不冲突 B3 hover |
| 后端 prompt 是否同时改成强制 ¶rank | 暂不改 | 等 Step 1 跑通后看精度需要 |
| RawLookup 历史数据 | 不迁移 | dev DB，M8 前再说 |
| 评测脚本（eval/runner.py）的 raw_lookup 用法 | 同步删 | 跟 Step 2 一起 |

---

## 六、风险点

1. **Tooltip 在 Flutter Web 的 hover 体验**：CanvasKit 渲染下 Flutter Tooltip 在 web 上有时显示延迟较长，需要实测；如果不达预期，B3 可降级到 B1（单击直跳，不要 tooltip）
2. **chunk 预览拉取**：hover 触发 `GET /chunks/{id}` 可能频繁触发请求，需要前端做 debounce + cache（300ms debounce + provider 自带 cache 就够）
3. **CitationInlineSyntax 放宽正则后误匹配风险**：现在的 `[(\d+\.\d+) §([\d\.]+)(?: ¶(\d+))?\]` 仍然要求严格的 `§` + spec 号 + section path 格式，普通方括号 `[note]` `[1]` 不会误中

---

## 七、不在本次范围

- 不动 reader page 内部锚点逻辑（已工作）
- 不动 citation 相关的 backend chunk schema
- 不改 LLM model / system prompt（除非 Step 1 后发现精度不行）
