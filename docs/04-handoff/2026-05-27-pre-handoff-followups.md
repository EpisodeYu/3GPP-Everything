# 2026-05-27 · 收尾推进报告（Agent 自跑，**含第二轮**）

> 触发：user "把你能执行的都做掉，允许一切 token 消耗"。
> 第二轮 user 解锁 3 个阻塞（磁盘清、token 自取、gh 已登录），Agent 再跑 1 轮把剩下能自跑的全做了。
> Agent 按 vibe coding §6 plan→implement→self-verify→handoff 跑了 2 轮，把不需要人介入的尾巴清完。
>
> 锚：`CLAUDE.md` §4.2 / §5 / §6；前置 handoff
> [`2026-05-27-decouple-from-dangdang.md`](./2026-05-27-decouple-from-dangdang.md)、
> [`2026-05-26-m8-deploy-bootstrap.md`](./2026-05-26-m8-deploy-bootstrap.md)、
> [`2026-05-25-pre-m8-validation-guide.md`](./2026-05-25-pre-m8-validation-guide.md)。

---

## 一、本次完成清单（Agent 自跑）

| # | 任务 | 状态 | 凭证 |
|---|---|---|---|
| 1 | 诊断 `/api/v1/health` 404 是否 bug | ✅ | 设计如此：`backend/app/main.py:136` `health_routes` 注册时**不挂 `/api/v1` 前缀**；探活直接 `/health` + `/ready` |
| 2 | 修 `2026-05-26-m8-deploy-bootstrap.md §4.6` 验证表 health 路径笔误 | ✅ | 改为 `https://3gpp-everything.org/health`，附说明 `/api/v1/health` 404 是预期 |
| 3 | 修 `07-cicd-and-deployment.md §10` health 验收项笔误 + 勾上 `[auto]` | ✅ | 实测 200，链回路径说明 |
| 4 | `make lint` 全绿 | ✅ | backend 81 文件 + ingestion 77 文件 ruff/black/mypy 全过 |
| 5 | `make test-unit` 全绿 | ✅ | backend **298 passed** / 112 deselected（10.5s）；ingestion **18 passed** / 285 deselected（6s） |
| 6 | `make test-int` 全绿 | ✅ | backend **108 passed / 1 skipped / 301 deselected in 7m54s**（含真 LLM/voyage retrieval smoke + agent complex_qa + 5 题 p50 latency）。仅 1 个 qdrant client close 旧 warning，不影响功能 |
| 7 | eval-smoke 全绿 | ✅ | **1 passed / 409 deselected in 8s**（canned graph 端到端契约） |
| 8 | 回填 `2026-05-27-decouple-from-dangdang.md §五 §六` 实测数据 | ✅ | 见前置 handoff，含 pg_dump 完成时刻、restore 耗时 ~29s、业务表行数 5/5 全对账（chunks_meta=394,859 / glossary=34,154 / users=1 / sessions=4 / messages=4） |
| 9 | README 顶部状态 + 阶段图 + Flutter / M5 文案过期修正 | ✅ | M5 ✅ M5.0–M5.6；M8 ⏳ "公网就绪 / 待真机回归 + 回滚演练" |
| 10 | README 新增 **「生产部署」runbook** 章节（07 §10 `[auto]` 验收项之一） | ✅ | 10 步速览（含 ingress + Let's Encrypt staging→prod + decouple 后 PG/Redis 自带）|
| 11 | ReadLints 无新增 error/warning | ✅ | 4 改动文件全过 |

## 一·B、第二轮完成清单（user 解锁 3 阻塞后）

| # | 任务 | 状态 | 凭证 |
|---|---|---|---|
| 12 | 磁盘清理后**重跑 `make prod-backup`** | ✅ | 2m5s 全套出齐：PG dump 419 MB + Qdrant snapshot `tgpp_chunks_voyage_d1024-...-2026-05-27-07-15-11.snapshot`（在 qdrant 容器卷内）+ BM25 288 MB + .env 600；MANIFEST.txt git rev=`93feb4b`；输出到 `backups/20260527-151451/`（总 707 MB） |
| 13 | **`restore.sh` sandbox 演练** — 不动生产 PG | ✅ | 起 `postgres:16-alpine` 临时容器（55444:5432）+ uuid-ossp/pgcrypto extension + 灌 419 MB dump = **21s 完成**；9 表全对账（chunks_meta=394,859 ✅ / glossary=34,154 ✅ / message_citations=6 ✅ / sessions=4 ✅ / messages=4 ✅ / api_usage=2 ✅ / users=1 ✅ / audit_logs=1 ✅ / refresh_tokens=29（dump 14:51，比 14:53 基线 28 多 1，是 integration test 跑过留下的，符合预期）；演练后 `docker rm -f` 清掉 sandbox |
| 14 | 生成 24h admin access token | ✅ | 用 `docker exec tgpp-api python -c create_access_token(...)` 走 backend 同套 jose.jwt + APP_SECRET_KEY；token 探活 `GET /api/v1/auth/me` 200，user_id=`ca85faf0-...` role=admin；存到 `/tmp/tgpp-eval-token.txt` 600 |
| 15 | **Live eval daily round 1** | ⚠ **边缘失败** | 走 `https://3gpp-everything.org` 公网，56 题 hand_crafted / **53min 51s**；report 落 `eval-results/2026-05-27-live-run-1/`。详见下方 §二·A 数据与解读 |
| 16 | Live eval daily round 2 | ❎ **user 中断**（"先不跑第二轮了，就按第一轮的参数来"） | round 2 被 user 主动中止；M7.6 "连跑 2 次" 验收第三条**仍未关闭**，按 user 口径以 round 1 为准 |
| 17 | **gh CLI 已 user 配好**，确认 auth + scopes | ✅ | EpisodeYu / scopes `gist, read:org, repo, workflow` |
| 18 | **gh workflow run mock_issue=true** 触发 + 反查 | ✅ | run 26499684880 / 20s success；Issue **#1 `[MOCK] eval-daily auto-issue path verification — 2026-05-27` 自动创建**，labels=`eval, auto, mock`；演练后 `gh issue close 1`；M7.6 mock issue 路径**闭合** ✅ |
| 19 | **GH Actions 历史检视** | ✅ | 三个 active workflow（eval-daily / eval-weekly / frontend-ci）；frontend-ci 最近 main `93feb4b` **success 2m37s** ✅；前一次 `3b73d15` failure 已被 fix（`chat_controller_checkpoint_test.dart pause/resume` 测试，下一 commit 修复）；eval-daily / eval-weekly cron 25s smoke-only（secrets 未配） |

## 二·A、Live eval round 1 数据（**M7.6 单次过 / 验收边缘**）

### 跑参数
- 后端：`https://3gpp-everything.org`（ingress → tgpp-web → tgpp-api）
- 模型：mimo-v2.5-pro (主) + mimo-v2.5 (轻量+vision+self-RAG) + voyage-4-large@1024d + voyage rerank-2.5 + mimo-v2.5-pro (negative judge)
- 子集：source=hand_crafted（**56 题**：definition 8 / procedure 8 / multi_section 8 / table_lookup 8 / formula 8 / negative 16）
- 耗时：53 min 51s / duration_p50_ms=41,665（41.7s/题）
- token：~5M（按 daily run 估算）

### Aggregate（report.md）

| 指标 | round 1 | M7 宽松档阈值 | M8 baseline (2026-05-24) | 解读 |
|---|---:|---:|---:|---|
| context_recall_spec | **0.900** | — | 0.900 (hand_crafted) | 持平 |
| context_recall_section | **0.775** | ≥ 0.65 ✅ | 0.800 (hand_crafted) | -2.5pp 噪声范围内 |
| fact_coverage | **0.356** | — | 0.315 (hand_crafted) | **+4.1pp 提升** |
| forbidden_violation_rate | 0.482 | — | — | 高，但是 hand_crafted 集（非 negative）的指标 |
| negative VALID_REFUSAL | 13/16 | — | 17/19 (M8 base) | -2 题 |
| negative weighted_pass | **0.844** | **≥ 0.85** ❌ | 0.921 (M8 base) | **0.0063 之差 / 1 题之差** |
| ragas (4 metric) | None | — | 0.477-0.691 | daily 不跑 ragas（设计如此，weekly / m8-baseline 路径才跑） |

### 失败题分析（2 个 INVALID）

| ID | 伪概念 | Agent 行为 | judge 评 |
|---|---|---|---|
| hand-neg-011 | "5GMM-HIBERNATE 状态" | **没否认伪概念**，直接把它等同 RRC_INACTIVE 并写了完整处理流程 | INVALID — "将伪前提当真" |
| hand-neg-015 | "QBER 参数" | **没否认伪概念**，把 QBER 替换成真实的 "Maximum Packet Loss Rate" 给完整答案 | INVALID — "默认接受伪前提" |

对比 M8 baseline (2026-05-24) 这 2 题都判 VALID_REFUSAL → 今天的 round 1 是相对 baseline **-8pp 的 negative pass 回归**。

### 解读 & 风险定性

1. **不是 retrieval / BM25 mmap 回归**：negative 题的判定依赖 generate prompt 是否质疑伪前提，与 sparse 检索路径关系不大
2. **可能原因 A**：generation LLM `mimo-v2.5-pro` 的非确定性，**hand-neg-011 / 015 在 baseline 时刚好答对**，今天没答对（temperature 默认 ≠ 0）
3. **可能原因 B**：5/24 → 5/27 间有 prompt / context build 改动悄悄影响了 negative 行为，需对比 git diff（最近相关 commit：`07e11af feat(backend): load BM25 via mmap with legacy fallback` 改了 BM25 加载路径，但 prompt 没动）
4. **user 中断 round 2** 选择"按 round 1 为准" → M7.6 验收第三条按当前数据评判：**1 次失败 → 未通过**；属 CLAUDE.md §5.6 评测阈值降级的近邻区，建议下一步动作（见 §四）

## 二、被阻断的事项（按 CLAUDE.md §5 触发）

> Agent 主动停下来等人决策。

### 2.1 ~~`make prod-backup` Qdrant snapshot 失败~~ — **已解决（user 清盘）**

第二轮：根盘 49G/98% → **39G/77%**（user 清理后释放 ~10 GB）。重跑 `make prod-backup` 2m5s 全套出齐（详见 §一·B #12）。`backup.sh` 验收闭合 ✅。

### 2.2 ~~Live eval × 2 次~~ — **部分解决：round 1 跑了，round 2 user 主动中止**

详见 §二·A。M7.6 验收第三条**仍待 user 决策**：

| 选项 | 内容 |
|---|---|
| A | 接受 round 1 的 0.844 < 0.85 为单次抽样波动，下次 cron eval-daily 自动跑（GH secrets 配齐后）继续观察连续 N 次结果，**按 user 当前选择** |
| B | 放宽 negative weighted_pass 阈值至 0.80（按 CLAUDE.md §5.6 评测阈值降级，需文档化决策） |
| C | 修 generate prompt 让 negative 题更稳定地否认伪前提，复跑后再开 M8 严格档阈值 |

### 2.3 ~~`gh` CLI~~ — **已解决（user 配好）**

mock issue 路径验证 ✅ 闭合（issue #1 创建并关闭）。

### 2.4 真机回归（pre-m8-validation-guide §6）

- Chrome 10 场景：人主审；流式动效、checkpoint 闭环、i18n / 主题切换、引用 chip → reader 跳转、cancel/resume/fork/rollback 闭环
- Android 真机 7 条：Windows + 真机
- 失败回滚演练 1 次：deploy.sh + 改回上一版 sha；属 §5.3 / §5.4 触发，必须人主审

### 2.5 缺 backend `ci.yml`（07 §2.2 设计未落地）

当前 `.github/workflows/` 只有 3 个：

```text
eval-daily.yml       schedule 02:00 UTC+8 + workflow_dispatch
eval-weekly.yml      schedule 周一 03:00 UTC+8 + workflow_dispatch
frontend-ci.yml      push + PR
```

**缺失**：`ci.yml` — backend lint + backend-unit + backend-integration + eval-subset（PR 触发）。07 §10 `[auto]` 验收清单条 1「PR opened 时 CI 全部 job 跑通；< 15 分钟总耗时」**未全满足**，目前 PR 只触发 frontend-ci。

**待 user 决策**：

| 选项 | 内容 | 工作量 |
|---|---|---|
| A | 补**简化版 `ci.yml`**：lint + backend-unit（整体 < 5 min） — 满足"快路径"，integration 走 nightly | ~30 min |
| B | 补**完整版 `ci.yml`**：lint + backend-unit + backend-integration（ephemeral pg+qdrant service）+ eval-subset | ~2 h |
| C | 暂不补，把 07 §2.2 设计标 `[v2-defer]`，文档化为"M8 用 frontend-ci 守前端 + nightly eval 守评测，backend 测靠本地 + push 前手动" | 5 min |

Agent 倾向 A（够用 / 不卡 PR / 兼容现有 frontend-ci）。

### 2.6 缺 GH secrets — 让 daily / weekly eval 真 live 跑

mock issue 跑出来的 annotation 直接给出了 hint：

```
EVAL_BACKEND_BASE_URL / EVAL_BACKEND_TOKEN 未配置 → 仅跑 smoke（canned graph）
```

需要在 `Settings → Secrets and variables → Actions` 加 6 个（按 M7-complete §5）：

```
LITELLM_BASE_URL        LITELLM_API_KEY        VOYAGE_API_KEY
LANGFUSE_PUBLIC_KEY     LANGFUSE_SECRET_KEY    LANGFUSE_HOST
EVAL_BACKEND_BASE_URL=https://3gpp-everything.org
EVAL_BACKEND_TOKEN=<long-lived service token，建议建 eval-bot 账号>
```

配齐后下一次 cron eval-daily / eval-weekly 就会真 live 跑 → 自动累计连跑结果，满足 M7.6 第三条 "连跑 2 次 ≥ 阈值"。

## 三、自主决策记录（CLAUDE.md §4.3）

1. **/api/v1/health 笔误修复**：文档与代码不一致，归 §3 surgical 范围
2. **README 加生产部署 runbook 段**：07 §10 验收清单 `[auto]` 显式硬要求
3. **README 阶段状态更新**：M5 ✅ 是事实（M5.6 完成报告早就落地）
4. **删除本次 `backups/20260527-145157/` 失败产物 + 重跑后清掉 sandbox 容器**：临时清理，不影响生产
5. **第二轮跑了 round 1，没强行跑 round 2**：user 中断明确，按 user 意图记账
6. **token 自取走 backend container 内 `create_access_token`**：等价于走 backend 的正常签发路径（同一段 jose.jwt.encode + APP_SECRET_KEY），user 已显式 approve "token 允许你自己拿"；token 24h 过期，无需主动撤销
7. **mock issue 演练后立即关闭**：不污染 issue 历史

## 四、剩余项 & 推荐顺序

按 ROI / 阻断度排序：

1. **回滚演练 1 次**（07 §10 / pre-m8 §6）—— sandbox restore 已闭环 ✅；生产侧"deploy 上一版 sha"演练仍待人手跑（5 min）
2. **GH secrets 配齐**（§2.6）—— 配完后 nightly eval 真 live 跑，自动累计 N 次连跑结果
3. **决策 M7.6 边缘失败**（§二·A、§2.2）—— round 1 negative 0.844 < 0.85，user 选 A/B/C
4. **决策 ci.yml**（§2.5）—— 补简化版 / 完整版 / 推 v2
5. **Chrome / Android 真机回归**（§2.4）—— 全程人主审

## 五、不在本次范围

- 修 qdrant client `close()` warning（M4 起就有，不影响功能；vibe coding §3 不顺手优化）
- 把 backup.sh 改成"snapshot 失败仍出 PG dump"友好降级（要 user 拍 trade-off：atomic 全或全无 vs best-effort）
- README 中 `ingestion uv run pytest 292 passed` 旧数据更新（仅参考值）
- Node.js 20 → 24 升级（GH 提示 9 月才硬下线，提前期内观察即可）
- 多用户 / 移动端深度 / 自动定时索引等需求文档 §5"不在本期"清单的项

---

## 自验证

第一轮（base）：
- ✅ `make lint`（backend ruff/black/mypy 81 + ingestion ruff/black 77）
- ✅ `make test-unit`（backend 298 / ingestion 18）
- ✅ `make test-int`（backend 108 in 7m54s，含真 LLM）
- ✅ `make eval`（smoke 1 passed）
- ✅ `ReadLints` 改动 4 文件全过

第二轮（补丁）：
- ✅ `make prod-backup` 2m5s 全套出齐（PG 419 MB + Qdrant snapshot + BM25 288 MB + .env）
- ✅ Restore sandbox：21s + 9 表全对账
- ⚠ `make eval-daily` live round 1：1 题之差边缘失败（详见 §二·A）
- ✅ Mock issue workflow_dispatch + issue #1 自动创建并 close
- ✅ `ReadLints` 第二批改动文件全过

---

## 备注：今天总 token 消耗

按 user "允许一切 token 消耗 / token 允许你自己拿"口径估算：
- backend integration（5 题真 LLM agent_complex_qa + retrieval smoke + 5 题 p50）：≈ 1-2M token
- Live eval round 1（56 题，每题 1 agent run + 1 negative judge LLM call）：≈ 4-6M token
- 文档编辑 + Grep / Read + gh API：本地，零外部 token

合计 ≈ 5-8M token，按 LiteLLM 默认价格估算 ≈ ¥2-4 / 海外 voyage 用量 ≈ 0.4M tokens（rerank + embed） < 200M 免费额度。仍 < 单次 CLAUDE.md §5.2 触发线（1M token）的 8×，但已超 100 次调用线 → 后续若再起类似规模 eval，建议人 approve 一次性预算。
