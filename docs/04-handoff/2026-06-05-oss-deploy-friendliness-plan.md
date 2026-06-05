# 开源部署友好度改造方案（外部用户 self-host）

> 2026-06-05 调查与方案。**本文档仅为方案，未动任何代码。**
> 触发：作为开源项目，外部用户 pull 源码自托管时存在多处阻断/高摩擦点。
> 决策记录：① 本轮先出书面方案，不实现；② 索引产物对外发布的 3GPP/GSMA 版权由人确认授权（前置条件，归人）。

## 0. 结论先行

外部用户自托管的障碍按"能不能跑起来"分三级。一个常见误判先澄清：

- **LLM 模型名其实没写死**：调用路径全走 `settings.LLM_*`（`backend/app/llm/litellm_client.py:201`），写死的只是 pricing 表、docstring 注释和**默认值**。真正硬锁的是 **embedding/rerank 供应商**（`config.py:67` `Literal["voyage","glm"]`）。
- **真正的病灶**有三：① compose 硬连 maintainer 私有 external network → fresh clone 直接起不来；② 索引产物（394k chunks）既不在库也无发布渠道 → 不可复现；③ embedding/rerank 强绑 Voyage。

## 1. 问题分级与证据

### P0 — 阻断级：fresh clone 根本起不来

| # | 问题 | 证据 | 后果 |
|---|------|------|------|
| P0-1 | compose 硬连 maintainer 私有 external network，且不自带 qdrant/litellm service | `deploy/docker-compose.yml:128-133`（`p2-rag-assistant_default` / `litellm_default`）；prod 同样 `docker-compose.prod.yml:203-208` | 外部机器无此网络，`docker compose up` 报 `network ... not found` |
| P0-2 | 仓库无 LiteLLM 配置，用户不知道怎么接上游 | `git ls-files \| grep litellm` 只有 Python client，无 `litellm/config.yaml`；`model_name`（`mimo-v2.5-pro`/`voyage-4-large`/`rerank-2.5`）→ 上游映射的隐性契约完全没文档化 | 即使自装 LiteLLM 也不知如何映射 |
| P0-3 | 生产部署引用仓库内不存在的私有项目 | `README.md:291`、`.env.example` 末尾：`cp ~/infra/ingress/.env.example` | `~/infra/ingress/` 是 maintainer 私有 repo，照抄即断 |

### P1 — 高摩擦：能起来但没数据 = 空壳

| # | 问题 | 证据 | 后果 |
|---|------|------|------|
| P1-1 ⭐ | 索引产物未上库，也无发布渠道，不可复现 | `backup.sh:52` 生成 Qdrant snapshot 但留在 maintainer 卷里从不对外发布；`restore.sh:46` 写"collection 由用户手动 restore"但无可下载产物 | 外部用户只能从零重跑 ingestion（Voyage key + 真金白银 + 文档载明 6–12h），README 徽章吹的 394k chunks 到不了 |
| P1-2 | Embedding/Rerank 硬锁 Voyage | `config.py:67` `Literal["voyage","glm"]`；rerank voyage-only（`rerank-2.5`），无关闭/换本地开关；collection 名由 `provider_d{dim}` 派生（`config.py:202`），换 provider = 索引作废 | 有 GPT-4 key 也被强制办 Voyage key |
| P1-3 | 默认模型指向小众国产模型 | `config.py:62-64` `mimo-v2.5-pro`/`mimo-v2.5`；judge `deepseek-v4-pro`/`glm-5.1` | 国际用户开箱拿不到 key |

### 非问题（不动）

- pricing 表对未知模型优雅降级（`pricing.py:150` `.get(model, _UNKNOWN_LLM)` → 成本算 0，不崩）。
- LLM 调用路径不写死模型，已是好实践。

## 2. 分阶段任务清单（可验证目标）

> 每阶段标了**验收标准**（CLAUDE.md §4 目标驱动）。纯文档/配置改动按 §4.1 例外可不加测试；涉及代码分支（阶段三）必须补测试。

### 阶段一：让 fresh clone 真能跑（解 P0）— ✅ 已落地（2026-06-05）

> 交付：`deploy/docker-compose.standalone.yml`（自带 qdrant+litellm+pg+redis，无 external
> network）、`deploy/litellm/{config.yaml,.env}.example` + `README.md`、Makefile
> `standalone-up/down/logs`、README 快速开始改 standalone + 生产部署标注 ingress 私有、
> `.gitignore` 忽略 litellm 实配。`docker compose config` 校验通过；未在本机 `up`（线上
> prod 占用 `tgpp-*` 容器名），干净机器验证命令见下方 T1.1 验收。

- **T1.1** 新增 `deploy/docker-compose.standalone.yml`：自带 `qdrant` + `litellm` + `postgres` + `redis` 全套，**不挂任何 external network**。保留现有 dev/prod compose（maintainer 复用宿主场景）。
- **T1.2** 新增 `deploy/litellm/config.yaml.example`：两份并列样例 ——（a）现行国产栈（`mimo-*`/`voyage-*`/`rerank-2.5`）；（b）OpenAI 栈（`gpt-4o-mini`/`text-embedding-3-large` 等），用户二选一。standalone compose 默认挂载之。
- **T1.3** 清掉 `~/infra/ingress/` 私有引用：README 生产部署段明确标注"ingress 为 maintainer 私有，外部部署请自备反代"，或抽一份最小 nginx+certbot 到 `deploy/ingress/`。
- **验收**：在一台**无任何宿主依赖**的干净机器上 `docker compose -f deploy/docker-compose.standalone.yml up` → `/health` 200、`/ready` 4 依赖联通（数据为空可接受，阶段二补）。

### 阶段二：让用户拿到数据（解 P1-1）— 🔨 工具已交付（2026-06-05），待发布

> 决策：渠道 = **HuggingFace Datasets**；repo = `EpisodeYu/3gpp-everything-index`。
> 交付：`scripts/export-index.sh`（产 bundle）、`scripts/bootstrap-index.sh`（一键恢复+校验）、
> `scripts/publish-index-hf.sh`（人执行的上传，带 PUBLISH 确认门）、`deploy/index/README.md`、
> README 索引侧加"选项 A 拉现成索引"、standalone qdrant 钉版升 `v1.17.1`（对齐生产，保证 snapshot 可 recover）。
> 已验证：三脚本 bash 语法、**PG dump 白名单零用户表泄漏**（只 chunks_meta+glossary）、qdrant snapshot 端点可达。
> 未跑：完整 export(~4G)→bootstrap E2E 与 HF 上传/下载往返（需人发布 + 干净机器，见 §3 剩余风险）。

> **前置（归人）**：确认 GSMA/3GPP 数据集授权允许再分发派生的 embedding 向量 + 原文片段。人已认领"可发布，我来确认授权"。授权未落实前不执行本阶段的对外上传动作。

- **T2.1** 把 Qdrant snapshot（`tgpp_chunks_voyage_d1024`）+ BM25 jsonl + `chunks_meta` 打成可下载产物，发布到 **HuggingFace Datasets 或 GitHub Release**。
- **T2.2** 新增 `scripts/bootstrap-index.sh`：一键下载 + restore 到本地 Qdrant + 解压 BM25 + 灌 `chunks_meta`。
- **T2.3** README/快速开始加"拿现成索引"路径，与"从零 ingestion"并列。
- **验收**：干净机器跑完 bootstrap 后，`index-status --provider voyage` 显示满量 points；问一题能拿到带引用的答案。

### 阶段三：拆供应商锁（解 P1-2 / P1-3）— 改动面最大

- **T3.1** `EMBEDDING_PROVIDER` 放开 `openai` / `local`（bge / sentence-transformers）分支；collection 名已带 provider+dim 天然隔离。
- **T3.2** 加 `RERANK_PROVIDER` + `RERANK_ENABLED`：允许本地 cross-encoder 或关闭走 RRF。
- **T3.3** `.env.example` 顶部加"国际用户最小配置"段，并列 OpenAI 一套默认。
- **验收**：以 `EMBEDDING_PROVIDER=openai`（或 local）+ rerank 关闭跑通端到端检索；新增分支有 unit 覆盖；`make lint` / `make test` 全绿。

## 3. 风险与注记

- **版权（阶段二）**：embedding 向量是派生数据，原文片段直接来自 GSMA HF 数据集；再分发前须核对授权，人已认领确认。
- **阶段三改 `EMBEDDING_PROVIDER` 语义**：属 `.env.example` key 含义变化，按 CLAUDE.md §8 需同步 `docs/03-development/01-infrastructure.md §2.4` 并走 §5.1 人审。
- **不向后兼容点**：换 embedding provider 会使现有 voyage@1024 索引失效——文档须显著提示"换 provider 必须重建索引"。

## 4. 下一步

待人选定执行范围（阶段一 / +阶段二 / 全套），再进入 plan→implement→self-verify→handoff 标准循环。本文档落地后按需更新完成度。
