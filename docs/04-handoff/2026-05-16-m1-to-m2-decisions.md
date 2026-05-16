# 2026-05-16 · M1 → M2 过渡决策记录

> 任务来源：人与 Agent 在 2026-05-16 下午对 voyage embedding 额度 / 双轨方案 / 维度策略 / 并行实施的协商对话。
> 上一份 handoff：[`2026-05-15-m1-poc-38331.md`](2026-05-15-m1-poc-38331.md)
> 报告作者：Agent（Cursor Claude Opus 4.7）；本文是**全局决策**人审记录，命中 `CLAUDE.md §5.1 / §5.5`，由人当面拍板。
> 性质：决策摘要 + 实施计划，**不含**任何代码改动结果（代码实施另起 handoff）。

---

## 0. TL;DR

四条决议在本文记录后生效，已同步到 `docs/02-tech-selection.md` §3 / §15、`docs/03-development/00-overview.md` §2 / §3、`docs/03-development/02-ingestion-and-indexing.md` §4.4 / §4.7 / §4.8、`docs/03-development/06-evaluation-and-observability.md` §8、`docs/03-development/01-infrastructure.md` §2.4、`.env.example`、`README.md`、以及本 handoff 系列前一篇 §6.4 / §8.3 / §10.5 的 banner。

| # | 决议 | 旧口径 | 新口径 |
|---|------|--------|--------|
| 1 | Embedding provider | voyage / 智谱 embedding-3 **双轨决胜** | voyage **单轨**；智谱仅代码 fallback |
| 2 | Embedding 维度 | 项目标准 = 2048（M1 §6.4 锁定） | M2 同时建 2048 + 1024 双 collection，**M3 ablation 决胜**；当前 2048 标识 `tentative` |
| 3 | M2 POC | 20 篇 voyage / glm 双轨 ~30M tokens（旧估） | 20 篇 voyage 单轨 + 双维度 **~8M tokens**（manifest 实测推算） |
| 4 | 索引并行 | 串行 `index_specs`（M1 现状） | M2 起 `pipeline_concurrent`：spec 级 worker=3 + vision fan-out concurrent=8 + 全局 voyage/mimo 速率器 |

**全量 token 预算订正**（按 38.331 实测密度 221k/MiB 推算 1271 篇 / 619.6 MiB raw.md）：

| 阶段 | tokens (M) | 累计 (M) |
|------|---:|---:|
| POC 38.331（1024 维老 collection 已花，drop 后不可复用） | 1.66 | 1.7 |
| B0 MRL 等价性 spike | 0.1 | 1.8 |
| M2 20 篇 POC multidim（含 38.331 重做 2048） | ~8 | ~10 |
| M3 评测期单 spec 调参重 embed（10-15 次） | 12-15 | ~22-25 |
| **M6 全量索引（1271 篇 × 一次 API 调用产 2048+1024）** | **150 ± 20** | **~175-185** |
| 上线后增量更新（半年内 Rel-20 等） | 5-10 | **~180-195** |
| **Voyage 200M 余量** | — | **5-20M (3-10%)** |

> 不留"全量重做一次"的余量。M3 评测期改 chunker 必须先在 20 篇 POC 上 ablation 验证（**M3 → M6 过渡硬指标**，详见 §3）。

---

## 1. 决策过程关键证据

### 1.1 维度对 token 消耗的影响 = 0

Voyage embedding 按 **input tokens** 计费，与输出维度（256/512/1024/2048）**无关**。1024 vs 2048 只影响 Qdrant 存储 + 检索 latency + 召回质量。

### 1.2 voyage-4-large 的 Matryoshka 性质

voyage-4-large 是 MRL 训练的模型：

> 一个 2048 维向量的前 N 维（N ∈ {256, 512, 1024}）截断 + L2 重 normalize，效果**几乎等价于**直接调 API 要 N 维。MTEB 等基准上差距通常 < 1%。

**工程含义**：M2 维度 ablation 只需调一次 API（output_dimension=2048），客户端 truncate 后落到两个 Qdrant collection。token 成本 **1×**，不翻倍。

但 MRL 等价性必须用真数据实测验证：**B0 spike** 用 38.331 已 chunk 出的 100 个 chunk 比对 A=truncate(2048) vs B=直接调 1024，门槛：

| 指标 | 门槛 |
|---|---|
| cosine median | ≥ 0.9995 |
| cosine min | ≥ 0.998 |
| token 成本 | ≤ 100k（≈ $0.012） |

不通过则 fallback 到"双调 API"，token 翻倍至 ~290M，**超 200M 额度 90M**，需要回到双轨决议环节。

### 1.3 全量 token 估算的实测基线

| 来源 | 数据 |
|---|---|
| 38.331 POC 实测 | 7.5 MiB raw.md → 1.66M embed tokens → **221k tokens/MiB** |
| 全量 manifest 实测 | 1271 specs / 619.6 MiB raw.md |
| 估算（最低 221k/MiB） | 136.9M |
| 估算（中位 250k/MiB） | 154.9M |
| 估算（上限 280k/MiB） | 173.5M（不现实，部分 ASN.1 密集系列拉低） |
| **采用中位** | **150 ± 20M** |

20 篇 POC 实际 manifest 数据（去重后 17 unique）：

| 项 | 值 |
|---|---|
| raw.md 合计 | 31.0 MiB |
| 估 tokens（221k/MiB） | 6.84M |
| 上限（280k/MiB） | 8.67M |
| 38.331 单篇占 | 7.5 MiB / 1.66M tokens（最大单篇） |

### 1.4 用户提供的运行时限速

| 资源 | 限速 |
|---|---|
| voyage-4-large（payment 已加） | **3M TPM / 2000 RPM** |
| mimo-v2.5（vision） | **10M TPM / 100 RPM**（瓶颈：100 RPM） |

> M1 POC §10.3 "voyage series 4 没有 free token"是 payment 未加时的状态；加 payment 后 200M 免费额度照常发挥。

---

## 2. 文档同步落点

本次决议落到以下文档（改动一并提 PR，不单独 review）：

| 文件 | 改动要点 |
|---|---|
| `docs/02-tech-selection.md` | §0 总表 embedding 行改 voyage 单轨 + GLM fallback；§3 整段重写（§3.1 决策、§3.2 ablation 方案、§3.3 重建路径、§3.4 并行架构、§3.5 Batch API 暂不启用）；§15 token 估算从 100M 改为 175-185M；§16 替换路径补 GLM 切换流程；§16 新增 MRL 等价性失效 fallback |
| `docs/03-development/00-overview.md` | §2 全局决策总表新增 Embedding provider / Embedding 维度 / Voyage 限速 / mimo 限速 / 索引并行 5 行；§3 mermaid + M2/M3/M6 表口径更新；§3 关键决策点新增 M3→M6 chunker 漂移门禁 |
| `docs/03-development/02-ingestion-and-indexing.md` | §1 交付物口径 1296→1271；§4.4 关键设计重写（单轨 + 多维度 + collection 命名规则 + Batch API 暂不启用）；§4.6 CLI 加 `--dimensions` `--concurrent` `--vision-concurrent`；§4.7 M2 / M6 段重写；§4.7 新增 M3→M6 过渡硬指标；§4.8 **新增**并发索引架构（限速基线 + 并发图 + 默认参数表 + MRL 实现要点 + 端到端预估 + 监控点）；§5 Qdrant 存储口径分 stable / ablation 两档；§6 监控点改 multidim 口径 + 新增限速指标；§8 验收清单 POC/生产两段全更新 |
| `docs/03-development/06-evaluation-and-observability.md` | §8 整段重写为"维度决胜"（vs 旧"provider 决胜"）；决胜规则差距阈值 5%→2%；新增 M3→M6 chunker 漂移门禁 |
| `docs/03-development/01-infrastructure.md` | §2.4 .env 表新增 `EMBEDDING_DIMENSIONS` / `VOYAGE_OUTPUT_DIMENSION` / `VOYAGE_TPM` / `VOYAGE_RPM` / `MIMO_TPM` / `MIMO_RPM` / `INDEX_CONCURRENT_WORKERS` / `INDEX_VISION_CONCURRENT` |
| `.env.example` | 同上 |
| `docs/04-handoff/2026-05-15-m1-poc-38331.md` | 顶部 banner：2026-05-16 决策更新，§6.4 / §8.3 / §10.5 的"2048 标准"为 tentative，正文不动（历史快照） |
| `README.md` | 顶部技术栈表 Embedding 行更新 |

---

## 3. M3 → M6 过渡硬指标（**新条款**）

> 写入 `docs/03-development/00-overview.md §3` + `docs/03-development/02-ingestion-and-indexing.md §4.7` + `docs/03-development/06-evaluation-and-observability.md §8`。

**条款**：

> M3 评测期间若 chunker 参数有任何改动（target_tokens / overlap / 注入头格式 / atomic_blocks 划分 / vision prompt 等会让 chunk content 字面变化的项），必须把改动后的 chunker 在 20 篇 POC 上重跑，与现有 Qdrant collection 的 chunk_id 集合做 diff：
>
> - **漂移率 ≤ 5%**：M6 可通过 `ingestion pipeline-hf --skip-indexed` 跳过 POC 20 篇，省 ~8M voyage tokens
> - **漂移率 > 5%**：视为"chunker 未稳定"，**禁止进入 M6 全量索引**；必须先在 20 篇上 ablation 确认指标改善，再决定是否全量重建 + 调整 chunker 锁定方案

**理由**：

- 200M voyage 余量仅 5-20M，全量重做一次（~150M）会爆额度
- chunk_id = `uuid5(spec_id + clause + sha256(content)[:16])`，content 任一字节变化都会让 ID 漂移 → POC 已花 tokens 报废
- POC §6.1 / §6.2 / §6.5 已经历过 chunker 修了 5 个 bug 才稳定的过程；M3 评测期还可能发现新 bug

**例外**：vision prompt 改动只影响 figure chunk，38.331 figure=64 占比 < 1%，全量影响极小；不必触发硬指标。但仍需在 handoff 里说清楚。

---

## 4. M2 实施计划（4 阶段）

### Task A: 文档同步 ✅ 本文产出

已在 §2 列清。零代码改动 / 零 token 成本。

### Task B: 并行 pipeline + 维度 ablation 代码（实施中）

#### B0: voyage MRL 等价性 spike（前置门槛）

- 输入：38.331 POC 已 chunk 出的 jsonl 取前 100 chunk
- 实验：A = voyage(2048) 截前 1024 + L2 renorm；B = voyage(output_dimension=1024) 直接调
- 比对：per-chunk cosine similarity
- 输出：`eval-results/m2-prep/voyage_mrl_equivalence.md`（含统计 + 决策结论）
- 门槛：cosine median ≥ 0.9995 / min ≥ 0.998
- 成本：~100k tokens / ~$0.012

#### B1: `ingestion/rate_limit.py`（新增）

- 异步 token bucket（aiolimiter 实现）
- 配置：voyage_tpm=3_000_000, voyage_rpm=2_000, mimo_tpm=10_000_000, mimo_rpm=100
- 提供 `voyage_limiter` / `mimo_limiter` 单例 + `with_rate_limit(tokens)` async ctx
- 单测：burst + 持续打满都不超 rate

#### B2: `ingestion/images/vision.py` 异步化

- 新增 `VisionResolver.aresolve_one(image_path, ctx) -> dict | None`（保留 sync `__call__`）
- 新增 `VisionResolver.aresolve_batch(items: list[(path, ctx)]) -> list[dict | None]`
  - 内部 `asyncio.gather` + semaphore（默认 8）
  - 全局 mimo limiter 透传
- chunker `figure.py` 调用方暂不改（保留同步入口），由 pipeline_concurrent 层调 aresolve_batch
- 单测：fakeredis + httpx.MockTransport，验证 fan-out 速率 ≤ 100 RPM / 8 concurrent

#### B3: `ingestion/indexer/embedder.py` + `qdrant_writer.py` 多维度

- `embedder.py`：
  - 新增 `Embedder.aembed_texts(texts) -> EmbeddingBatchResult` 异步入口（保留 sync）
  - 调用时显式传 `output_dimension=2048`（LiteLLM proxy 已声明，再传一次显眼）
  - 客户端按 `EMBEDDING_DIMENSIONS` env 切两份向量：vec_2048 = response；vec_1024 = `[normalize(v[:1024]) for v in vec_2048]`
  - 返回 `MultiDimEmbeddingResult(vectors_by_dim: dict[int, list[list[float]]], dim_main: int, prompt_tokens: int)`
- `qdrant_writer.py`：
  - 新增 `QdrantWriter.upsert_multidim(chunks, vectors_by_dim: dict[int, list[...]])`
  - 内部按 dim 维护多个 collection（命名 `{prefix}_{provider}_d{dim}`）
  - `ensure_collection` 改为 `ensure_collections(dims: list[int])`
- 单测：truncate 后向量 norm ≈ 1.0；与 voyage 1024 API 等价（B0 数据驱动）；upsert_multidim 在 fakeredis/embedded qdrant 上写入正确 collection

#### B4: `ingestion/indexer/pipeline.py` 并发 + CLI

- 新增 `pipeline_concurrent(spec_ids, components, *, workers=3, vision_concurrent=8, dims=[2048,1024]) -> PipelineStats`
  - `asyncio.Queue` 分发 spec
  - 每 worker 异步流水：load → chunker（vision batch fan-out） → embed multidim → upsert multi-collection
  - 失败重试 + dead-letter 写 `INGEST_DATA_DIR/failed/`
  - 实时回填 `voyage_tpm_used` / `mimo_rpm_used` 到 PipelineStats
- 保留旧 sequential `index_spec` / `index_specs`（CI / 单 spec 调试用）
- CLI：`pipeline-hf` 加 `--concurrent N --vision-concurrent M --dimensions 2048,1024`
- 集成测：38.331 用 concurrent 重跑，验证 chunk_id 集合与 sequential 输出一致

### Task C: self-verify

- `make lint` + `make test`（unit + integration） + `ReadLints` 全绿
- 38.331 在 concurrent pipeline + multidim 下重跑一次，verify：
  - `_d2048` + `_d1024` 两 collection 各 8853 points
  - chunk_id 集合与 sequential 输出 1:1 等价
  - BM25 + PG 计数一致
  - 耗时降到 < 60% 原 sequential 时间（实测目标 5-10 min）

### Task D: 20 篇 POC（人审 §5.2 后执行）

- 估 ~8M voyage tokens / ~$0.96（落 200M 免费区内）
- 估端到端 1-2h（3 并发 worker + multidim）
- 输出：`eval-results/m2-poc/20specs_index_stats.json` + `20specs_throughput.md`（实测速率回填）
- **不直接跑**，等本文 handoff review 后人显式批准

---

## 5. 风险与排雷

| 风险 | 触发 | 应对 |
|---|---|---|
| B0 MRL 等价性失效 | cosine median < 0.9995 或 min < 0.998 | 暂停 implement，回头与人重谈：(a) 接受 1024 单维度跑 + 放弃 2048；(b) 接受双调 API + token 翻倍 + 重新评估 200M 是否够 |
| voyage TPM 实际比 3M 低 | 跑 38.331 速率 < 1M TPM | 在 LiteLLM proxy log 查上游限速；动态降并发；可能需要联系 voyage 调额度 |
| 2 核 CPU 在 worker=3 下 load > 5 | 监控触发 | 降 worker=2，handoff 备注 |
| mimo proxy 单进程吞吐瓶颈 | vision fan-out 实测速率 < 50 RPM | 查 LiteLLM 进程数 / async 配置；或降到 vision_concurrent=4 |
| Qdrant 双 collection 上 spec 时 dim mismatch | upsert_multidim 写错维度 | 单测覆盖；集成测复跑 38.331 multidim 必过 |
| chunker 输出非确定性导致 multidim 间 chunk_id 集合不一致 | 同 spec 多次跑 chunker 得到不同 chunks | 已确认 chunker 在同 input 下输出确定（uuid5 内容稳定）；如未来引入随机性需单独防御 |
| chunker 仍有 P3 bug 未修 → 影响 M3 决胜 | M3 评测时发现 chunker 还有问题 | M3 → M6 过渡硬指标兜底；最坏退到"清掉 POC + 用修好的 chunker 重跑全量"，吃掉 200M 余量 |

---

## 6. 与上一份 handoff 的口径差异

[`2026-05-15-m1-poc-38331.md`](2026-05-15-m1-poc-38331.md) 中的几处口径在本次决议后变化（**该文档不改原文，仅顶部加 banner 指向本文**）：

| 上文位置 | 旧口径 | 新口径 |
|---|---|---|
| §6.4 | "项目标准 = 2048 维"已锁定 | **tentative**，M3 决胜后再锁 |
| §8.1 | "20 篇双轨估算 voyage + glm 总成本 ~$14" | 改 voyage 单轨 ~$0.96（甚至落免费区 = $0） |
| §8.3 | "已统一以 2048 维为项目标准" | **暂行**，等 M3 |
| §10.5 | "项目统一配 2048（2026-05-16 起）" | **暂行**，M3 ablation 决胜后再决定 |
| 老 collection `tgpp_chunks_voyage` 1024 维 | M2 multidim 复跑前 drop 重建 | **不变**，但新 collection 命名改 `tgpp_chunks_voyage_d{2048,1024}` |

---

## 7. 后续行动

1. ✅ **Task A 文档同步**完成（本文产出）
2. ⏳ **Task B0 spike**：等用户明确"可以花 100k tokens"后开始
3. ⏳ **Task B1-B4 代码实施**：B0 通过后进行
4. ⏳ **Task C self-verify**：B 完成后
5. ⏳ **Task D 20 篇 POC**：C 通过后回到这里报批 §5.2（~$1）

完成后另起一份 `2026-05-XX-m2-concurrent-pipeline.md` 记录代码实施细节 + 测试覆盖 + 实测速率。

---

_本文档由 Agent 在决策落地时生成，记录决策本身不引入新决策。文档与代码相互引用的地方，**改一处必检另一处**（CLAUDE.md §8）。_
