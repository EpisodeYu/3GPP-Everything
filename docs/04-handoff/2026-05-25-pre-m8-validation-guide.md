# 进 M8 前真机端到端验证手册

> 触发：M0–M7 全部完工（2026-05-25 M5.6 收尾），M8 上线前需要做一次完整端到端真机回归。
> Agent 已帮你准备好 dev box 环境；你按本手册一步步跑 Chrome / Android 用例即可。
>
> 任何一步失败：把 5–10 行错误日志 / 截图发给 agent，agent 会定位修复。

---

## 0. 当前环境快照（Agent 已就位）

| 类别 | 服务 | 状态 | 验证命令 |
|---|---|---|---|
| API | `tgpp-api` (uvicorn :8002) | ✅ Up + /ready 4/4 | `curl http://127.0.0.1:8002/ready` |
| Web | `tgpp-web` (nginx :8082, M5.6 真镜像) | ✅ Up + SPA fallback | `curl -I http://127.0.0.1:8082/admin` 应 200 |
| PG | `dangdang-postgres` (:5432, db=`tgpp_everything`) | ✅ healthy | `docker exec dangdang-postgres pg_isready` |
| Qdrant | `qdrant` (:6333, collection `tgpp_chunks_voyage_d1024`) | ✅ green, 394859 points | `curl http://127.0.0.1:6333/collections/tgpp_chunks_voyage_d1024` |
| Redis | `dangdang-redis` (:6379 db=5) | ✅ Up | `docker exec dangdang-redis redis-cli ping` |
| LiteLLM | `litellm` (:4000) | ✅ Up | `curl http://127.0.0.1:4000/health/liveliness` |
| Langfuse | Cloud `https://cloud.langfuse.com` | ✅ keys 配齐 | 浏览器登录看 dashboard |

**数据**：
- chunks_meta: **394,859 条**（PG）
- Qdrant points: **394,859**（点对点对齐）
- 已索引 specs: **1,270 个**（5G 系列 TS，覆盖 R18+R19）
- 38.331 = 10,695 chunks（最大）；23.501 = 2,400 chunks；32.255 = 1,070 chunks 等

**账号**：
- 唯一 admin = `s1yu` / `<REDACTED>`（已 truncate 旧账号 cascade，干净起步）

**已 fixed 的两件事**：
1. `tgpp-web` 容器从 M0 占位页 → 切到 M5.6 真 Flutter web 镜像
2. `.env` `ALLOWED_ORIGINS` 加 `http://localhost:8080`，让 `make web-run`（dev 模式）也能 CORS 通过

---

## 1. 启动方式（两套选其一）

### A. 容器模式（已经跑着，最少操作）

```bash
# Web 入口（远程访问也行：把 localhost 换成 dev box IP）
open http://localhost:8082/

# 后端 API（远程访问：经由 :8082/api/v1 同源反代，无需暴露 :8002）
curl http://localhost:8002/health
```

**适用**：日常验证、Chrome smoke、Android 真机连本机 8002。

> 2026-05-26 修：`tgpp-web` 镜像现在以同源相对路径 `/api/v1` 调 API，由 `frontend/nginx/default.conf` 反代到容器 `api:8002`。所以**从非 dev-box 机器**访问 `http://<dev-box-ip>:8082/` 也能用，不会再出现"加载文档列表失败：DioException [connection timeout]"。改动同步：`frontend/nginx/default.conf` 加 `/api/v1/` location；`Makefile` 加 `WEB_DOCKER_API_BASE_URL ?= /api/v1` 默认。

### B. 前端 dev 模式（要热重载 / debug）

```bash
make web-run                              # → http://localhost:8080
# 后端复用容器里的 tgpp-api (:8002)，CORS 已开 8080
```

**适用**：调样式 / DevTools 实时看流。**别**同时开 A+B 的浏览器 tab 走不同端口测同一 session，会因为 CORS 实测细节让人困惑。

---

## 2. Chrome 端到端验证（核心 10 个场景）

> 先 `open http://localhost:8082/`，用 `s1yu` / `<REDACTED>` 登录。每个场景独立可重复。

### 场景 1：登录 + 鉴权 +  i18n + 主题切换（M5.0 / M5.6）

| 步骤 | 期望 |
|---|---|
| 进 `/` → 重定向 `/login` → 输 `s1yu` / `<REDACTED>` → 点登录 | 跳到 `/chat` 欢迎页，sidebar 底部显示 `s1yu` / `role=admin` |
| sidebar 顶部点 **翻译图标** → 选 `English` | 所有可见文案翻成英文（"New session" / "Reader" / "Admin"） |
| 再点翻译图标 → 选 `中文` | 翻回中文 |
| sidebar 顶部点 **主题图标** → 选 `深色` | 全页面变深色 |
| **刷新浏览器** | 主题/语言保留（shared_preferences 持久化） |
| 选 `跟随系统` | 浏览器 `prefers-color-scheme` 决定主题 |

**失败信号**：CORS 报错 / 切换不生效 / 刷新被打回 system。

### 场景 2：会话 CRUD（M5.1）

| 步骤 | 期望 |
|---|---|
| 点 sidebar **新会话** | 自动跳到 `/sessions/<sid>`，sidebar 列表多一条 |
| 在新会话 sidebar 行点右侧 ⋮ → 重命名 → 输 `测试 1` → 保存 | sidebar 标题更新；刷新仍是 `测试 1` |
| ⋮ → 删除 → 确认 | 列表移除，跳回 `/chat` 欢迎页 |
| 在 Chrome DevTools Network 看 `DELETE /sessions/<sid>` | 204 No Content |

### 场景 3：SSE 流式问答 + 引用 chip + 节点状态条（M5.2 / M5.3）

| 步骤 | 期望 |
|---|---|
| 新会话 → composer 输入 `What is PDU Session Establishment in 5G?` → 回车发送 | 顶部 `[classify ✓] [retrieve ✓] [rerank ⟳]…` 状态条逐个变色 |
| 抽屉自动弹出 chunks 预览 | 显示 5–10 个候选；rerank 完成后带 `rerank_score` |
| assistant 气泡内 token 流式累积 | 每秒看到新 token；偶尔 `[23.501 §5.6.1 ¶3]` 这种引用 chip |
| 点引用 chip | 底部弹出 sheet：spec_id / section_path / chunk 内容预览 + "跳到完整章节"按钮 |
| 点 "跳到完整章节" | 跳 `/reader/23.501/5.6.1.2#chunk-<id>`，对应 chunk 临时高亮 3 秒淡出 |
| reader 左侧 toc drawer 点别的 section | 中央内容换；URL `/reader/23.501/<新 section>` |
| reader 顶部搜索框输 `gNB` → 回车 | 搜索结果列表 |

**Agent 已验证 SSE 链路通**（classify 3s → retrieve → rerank 0.8s → generate 11s → self_rag 1s），前端只要 SSE parser 没坏就能跑。

### 场景 4：取消 + 错误恢复（M5.2）

| 步骤 | 期望 |
|---|---|
| 发问后 token 还在流时 → 按 composer **取消** 按钮 | assistant 气泡顶部小角标显示"已取消"，已生成内容固化；按钮回到发送状态 |
| 同会话再发一条 | 新 run，旧的 cancelled 消息留着 |
| Network 面板看 `POST /sessions/<sid>/runs/<rid>/cancel` | 204 |

### 场景 5：Checkpoint 闭环 — 暂停 / 恢复（M5.4）

| 步骤 | 期望 |
|---|---|
| 发问后 token 还在流时 → 按 **暂停** | 出现 "已暂停 · 点击恢复" 横幅，按钮变 `恢复 / 取消` |
| 关闭浏览器 tab | （checkpoint 已持久化到 PG `checkpoints` 表） |
| 重新进同一会话 | 横幅仍在；点 **恢复** → SSE 续跑后续节点（generate / self_rag），token 接着流 |

### 场景 6：Checkpoint 闭环 — 分叉 / 回滚（M5.4）

| 步骤 | 期望 |
|---|---|
| 在已完成对话里 **长按一条 user 消息** → 选 "从这里重问" → 输新问题 → 提交 | 跳到新会话 `/sessions/<new sid>`；老会话 sidebar 移到底部 **分叉历史** 分组（灰度），打开看是只读 + 顶部"回到主线"按钮 |
| 点 "回到主线" | 跳回新会话 |
| 设置菜单 → "删除最后 N 轮" → slider 选 2 → 二次确认 | 最后 2 轮消息消失，会话状态正常 |
| **长按 assistant 消息** | 弹菜单：复制全文 / 复制 markdown / 👍 👎 / 收藏 / 笔记 |
| 选 "收藏" / "笔记" | toast 成功；DB `favorites` / `notes` 各加一条（DevTools 不直接看，可走 Network 面板） |

### 场景 7：管理后台 RBAC + 4 个 Tab（M5.5）

| 步骤 | 期望 |
|---|---|
| sidebar 点 **管理后台** | 跳 `/admin`；4 个 Tab：文档 / 任务 / 统计 / 工具 |
| **文档 Tab** → release 输 `Rel-18` → 回车 | 列表过滤（注意：M5.5 实测中 `documents` 表本期是空的，`/docs` 走 chunks_meta 聚合不依赖 release/series 字段；输 `Rel-18` 可能匹不到，留空看完整 1270 条更直观） |
| **统计 Tab** | 显示 `chunks=394859`, `users=1`（其余字段视近 7 天活动） |
| **工具 Tab** → "Langfuse 控制台" | 新标签页打开 `https://cloud.langfuse.com` |
| **工具 Tab** → "重建索引" → 输 spec_id `23.501` + 不勾 force → 提交 | snackbar 提示去任务页；**任务 Tab** 出现一条 queued/running 任务，3 秒自动刷新 progress |

**RBAC 兜底**：临时用 SQL 把 `s1yu` 改成 `role='user'` → 刷新 → sidebar `管理后台` 入口消失 → 地址栏敲 `/admin` 自动跳 `/chat`（验证完记得改回 admin）。命令：

```bash
docker exec dangdang-postgres psql -U tgpp_app -d tgpp_everything -c "UPDATE users SET role='user' WHERE username='s1yu';"
# 验证完
docker exec dangdang-postgres psql -U tgpp_app -d tgpp_everything -c "UPDATE users SET role='admin' WHERE username='s1yu';"
```

### 场景 8：阅读器独立路径（M5.3）

| 步骤 | 期望 |
|---|---|
| sidebar 点 **阅读器** → 弹文档选择对话框 → 输 `23.501` 过滤 → 选 23.501 | 跳 `/reader/23.501`，左侧 toc drawer 展开章节树（10–50+ 节） |
| toc 点 `5.6.1.2` 这种叶子 | 中央渲染对应章节 markdown，URL 变 `/reader/23.501/5.6.1.2` |
| 右上角搜索框 `gNB` | 命中结果列表 |
| 地址栏直接 paste `http://localhost:8082/#/reader/38.331` | 直接进 38.331 阅读 |

### 场景 9：Markdown / LaTeX / 表格（M5.2 / M5.3）

发问：

> Show me the formula for PDU Session lifetime in 5G and a comparison table.

（agent 是否真的输出 LaTeX 取决于查询，看 generate 节点的 output）

期望：
- 块级 `$$ ... $$` 显示渲染后的公式（不是原始 latex）
- markdown 表格正常列对齐
- 复制粘贴 chip 文本到剪贴板可用

### 场景 10：登出 + 重登（M5.0 / M5.1）

| 步骤 | 期望 |
|---|---|
| sidebar 右下角点 logout 图标 | 跳 `/login`；DevTools `localStorage` 清掉 token |
| 再登录 | 状态从 0 开始，之前会话仍在 sidebar 列表（PG 持久化） |

---

## 3. Android 真机验证（Windows 上跑）

### 3.1 环境准备（你的 Windows 上一次性）

```powershell
# 1. 装 Flutter 3.44.0（与服务器版本对齐）
#    下载 https://docs.flutter.dev/get-started/install/windows
#    解压到 C:\dev\flutter，把 C:\dev\flutter\bin 加 PATH
flutter --version       # 应显示 3.44.0

# 2. 装 Android SDK + 给真机开 USB 调试
flutter doctor          # 跟它的提示装齐 Android Studio / NDK / Java

# 3. 拉项目
git clone git@github.com:EpisodeYu/3GPP-Everything.git
cd 3GPP-Everything\frontend
flutter pub get
```

### 3.2 找到 dev box 的局域网 IP

在 dev box 上跑：

```bash
hostname -I | awk '{print $1}'   # 例如 192.168.1.100
```

**前提**：dev box 和 Android 真机必须在同一 WLAN/局域网；如不能，用 `adb reverse tcp:8002 tcp:8002` 把 USB 反向代理（手机访问 `localhost:8002` 转到电脑）。

### 3.3 Build APK + 装

```powershell
# IP 模式（手机和服务器同网）：
flutter build apk --release ^
  --dart-define=API_BASE_URL=http://192.168.1.100:8002/api/v1 ^
  --dart-define=LANGFUSE_URL=https://cloud.langfuse.com

# 或 USB 反代模式：
flutter build apk --release ^
  --dart-define=API_BASE_URL=http://localhost:8002/api/v1 ^
  --dart-define=LANGFUSE_URL=https://cloud.langfuse.com
adb reverse tcp:8002 tcp:8002

# 装到真机：
adb install -r build\app\outputs\flutter-apk\app-release.apk
```

### 3.4 真机用例（精简 7 条 = Chrome 场景的子集）

按手机 home 划进 app 后：

1. **登录** `s1yu` / `<REDACTED>` → 进 chat 欢迎页
2. **新建会话** → 发问 `What is HARQ?` → 看 SSE token 流 + 引用 chip
3. **取消** 跑到一半的 run → 状态回到 idle
4. **点引用 chip** → bottom sheet 弹出 → 跳 reader → 看锚点高亮
5. **长按 assistant** → 收藏 → 返回 chat 看 sidebar 状态正常
6. **侧栏 ⋮ 删除会话** → 跳回欢迎页
7. **切深色主题 + 切英文** → kill app 重开 → 偏好保留

**已知限制**：Android 后台 SSE 可能被回收（M5 v1 不解决，docs §13 已说）；保持 app 前台跑就行。

---

## 4. 把 M7 唯一尾巴闭环：live eval × 2 次

> 这是 M7.6 留的 `[blocked-on-deploy]`：连跑 2 次 daily live eval，确认阈值过 + Langfuse trace + 不产生 nightly-fail issue。你说"M8 之前跑"，agent 跑给你看。

```bash
cd /home/s1yu/3GPP-Everything

# 拿一个 24h 长效 token 给 EVAL_BACKEND_TOKEN（local 走 8002 不走 HTTPS，避免短 token 中途过期）
TOKEN=$(curl -sS -X POST http://127.0.0.1:8002/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"s1yu","password":"<REDACTED>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo $TOKEN

# 第一次跑
EVAL_BACKEND_BASE_URL=http://127.0.0.1:8002 \
EVAL_BACKEND_TOKEN=$TOKEN \
RUN_LIVE_EVAL=1 \
EVAL_REPORT_DIR=eval-results/pre-m8-run-1 \
make eval-daily 2>&1 | tail -20

# 第二次跑（等第一次完了再跑）
EVAL_BACKEND_BASE_URL=http://127.0.0.1:8002 \
EVAL_BACKEND_TOKEN=$TOKEN \
RUN_LIVE_EVAL=1 \
EVAL_REPORT_DIR=eval-results/pre-m8-run-2 \
make eval-daily 2>&1 | tail -20

# 看报告
cat eval-results/pre-m8-run-1/report.md
cat eval-results/pre-m8-run-2/report.md
```

**门槛**（M7 宽松档，详见 `docs/03-development/06-evaluation-and-observability.md §7`）：
- faithfulness ≥ 0.75
- context recall ≥ 0.65
- answer relevancy ≥ 0.70
- answer correctness ≥ 0.55
- latency-p50 ≤ 6s
- cost-p50 ≤ ¥0.30

**预算**：daily 子集 ≈ 20 题，单次 ~¥1–2 / ~5–10 min。两次合计 ~¥2–4。

> 如果你希望 agent 帮你跑、直接给报告，告诉我"跑 live eval"，我接着干。

---

## 5. 出问题怎么 collect 信息

任何场景失败，把下面 4 块发回来就够 agent 定位：

```bash
# (a) tgpp-api 最近 50 行
docker logs --tail 50 tgpp-api

# (b) /ready
curl -sS http://127.0.0.1:8002/ready

# (c) Chrome DevTools Network 面板对失败请求右键 → Copy as cURL
# 把 cURL 粘出来

# (d) 失败截图（含 URL bar 与 Console error）
```

Android 出问题：

```bash
adb logcat | grep -iE "tgpp|flutter" | tail -100
```

---

## 6. 进 M8 的硬门禁清单

完成下面所有 → 才正式开 M8：

- [ ] Chrome 场景 1–10 全过（**人主审**）
- [ ] Android 真机 7 条全过（**Windows + 真机**）
- [ ] live eval × 2 次 ≥ M7 宽松阈值（**本机跑或让 agent 跑**）
- [ ] frontend-ci workflow push 后第一次跑绿（GH Actions 上看）
- [ ] mock issue 路径验证：`gh workflow run eval-daily.yml -f mock_issue=true` → GitHub 出现一条 `[MOCK] eval-daily auto-issue path verification` issue（验完关掉）

发现 bug：
- 关键 bug → fix 后再 commit + 跑回归再继续
- 体验小问题（拼写 / 边距）→ 列 issue，M8 / 后续迭代再处理，不阻塞上线

---

## 7. M8 的工作范围（这些**不**在本验证手册）

M8 接管：
- 域名 + DNS（你买 / 配）
- 生产 Compose `deploy/docker-compose.prod.yml`
- Nginx 反代 + Let's Encrypt HTTPS（`deploy/nginx/tls.conf`）
- 备份 / 回滚 / Runbook 演练
- GH Secrets 配 `EVAL_BACKEND_BASE_URL=https://<域名>` + 长期 service token 让 GH Actions nightly eval 跑 live

本手册只覆盖 **dev box 真机功能验收**，给 M8 一个干净的起点。
