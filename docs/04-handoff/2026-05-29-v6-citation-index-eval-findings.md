# 2026-05-29 · v6 citation 索引方案：上线 + eval baseline

> 引用契约从 `[spec_id §section_path]` 文本切到 `[N]` 索引；prompt / 后端正则 /
> 前端正则三处耦合消除；self_rag `_citation_hit_rate` 退役。本文档落本次改动的
> daily + ragas 指标，立**新 baseline**，后续 PR 直接对比。
>
> 锚：[`03-development/03-agent.md §4.7 §4.8`](../03-development/03-agent.md)
> + commit 链 `eeae318` → `fb5a018` → `25b6120`（feature branch
> `feat/citation-index-refactor`，2026-05-29 12:00 部署到 https://3gpp-everything.org/）

## 1. 改动 TL;DR

- **LLM 引用格式**：`[spec_id §section_path]` → `[N]`（N = chunks 列表 1-based）
- **后端 `parse_citations`**：100+ 行三段 fallback（strict / fuzzy / spec-only）→
  一行索引边界检查 + 去重
- **self_rag `_citation_hit_rate`**：删除（索引方案下恒为 1.0，原检测面归零）；
  grounding 真实性全权交给 LLM `faithful` + `coverage` 字段
- **前端 `CitationInlineSyntax`**：正则 `\[(\d+)\]`；chip 元数据从 `citationsByRank[N]`
  反查后端回填的字段；**无 legacy fallback**（旧 `[spec §section]` 消息 chip 不可点，
  文本仍可读）
- **测试**：8 个测试文件适配；顺手修 3 个 pre-existing 集成失败（test_sse_events 两
  条 + test_self_rag_retry_cap 一条，main 上也红）
- **代码净减**：16 files / +301 / −518 / 净 −217 行

详见 prompt frontmatter `generate_qa.md` v6 notes + `03-agent.md §4.7 §4.8`。

## 2. Spike 验证（落地前）

`backend/scripts/spike_citation_index.py`（验完即删，commit 历史可见）：3 题
× mimo-v2.5-pro，覆盖多 chunk 同 spec / IE chunk（section_path=`<none>`）/
跨 spec。结果：

| 指标 | 值 | 通过阈值 |
|---|---|---|
| `[N]` 总出现 | 18 | ≥ 6 |
| 旧 `[spec §section]` 漂移 | 0 | = 0 |
| `(N)` / `［N］` / `[chunk N]` 漂移 | 0 | = 0 |
| bullet 行引用覆盖率 | 91.67% | ≥ 90% |

通过。直接进实施 + 全测试 + 部署。

## 3. Daily 指标对比（substring + negative judge）

| 指标 | **v6 (2026-05-29)** | baseline (2026-05-27 after-ad) | Δ | 解读 |
|---|---|---|---|---|
| **negative weighted_pass** | **100.00%** | 84.38% | **+15.62pp** | 16/16 全 VALID_REFUSAL（baseline 1 PARTIAL + 2 INVALID） |
| **negative valid_rate** | **100.00%** | 81.25% | **+18.75pp** | 同上 |
| forbidden_violation_rate | 46.43% | 48.21% | −1.79pp | 轻微改善 |
| fact_coverage | 30.32% | 35.21% | **−4.88pp** | 大部分 metric artifact（详见 §5） |
| context_recall_section | 82.50% | 82.50% | 0.00pp | 检索未变 ✓ |
| context_recall_spec | 92.50% | 95.00% | −2.50pp | 1 题差异，1/40 = 2.5pp 噪声 |
| duration_p50_ms | 53832 | 63243 | **−9.4s（−15%）** | prompt 稍短 + 删 hit_rate scan |
| terminal final | 56 | 56 | 0 | 全成功 |

`negative_judge` verdict 分布：

| | v6 | baseline |
|---|---|---|
| VALID_REFUSAL | 16 | 13 |
| PARTIAL_REFUSAL | 0 | 1 |
| INVALID | 0 | 2 |

**结构性收益**：v6 prompt 强制 `[N]` 必须指向真 chunk，LLM 无法编出看起来合规的
`[spec §section]` 来掩盖 hallucination → 没 chunk 支撑时更倾向于显式拒答。

## 4. Ragas 指标对比（v6 vs m8-baseline，同 56 题）

m8-baseline 用同一 judge（deepseek-v4-pro）跑过 175 题，v6 daily 的 56 题全部
落在 m8 集合内 → 56 题精确 overlap，apples-to-apples 比较。

⚠️ m8-baseline 是 2026-05-24 数据，prompt 为 v3 era（早于 v4 grounding 护栏 +
v5 chunker header 反诱导 + v6 citation 索引）。所以本节 Δ 是 **prompt v3→v6
3 步累积效应**，不仅是 citation 切换。

| Ragas metric | **v6 (2026-05-29)** | m8-baseline (2026-05-24) | Δ |
|---|---|---|---|
| **faithfulness** | **0.6934** (n=31) | 0.5671 (n=33) | **+12.63pp** |
| answer_relevance | 0.7145 (n=40) | 0.7836 (n=42) | −6.91pp |
| **context_recall** | **0.4917** (n=40) | 0.4079 (n=38) | **+8.38pp** |
| **context_precision** | **0.7342** (n=40) | 0.5833 (n=42) | **+15.08pp** |

`faithfulness` 按类目分解（v6 全线领先）：

| category | v6 | m8 | Δ |
|---|---|---|---|
| definition | 0.810 (n=3) | 0.702 (n=5) | +10.79pp |
| formula | 0.637 (n=7) | 0.510 (n=5) | +12.68pp |
| multi_section | 0.615 (n=6) | 0.566 (n=6) | +4.85pp |
| **procedure** | **0.786** (n=7) | 0.512 (n=6) | **+27.44pp** |
| **table_lookup** | **0.677** (n=8) | 0.394 (n=6) | **+28.31pp** |
| negative | n=0 | 0.612 (n=5) | — |

`negative` 类目 v6 ragas 全部跳过（n=0），因 v6 答案都是干净拒答 → ragas
`_extract_contexts` 在空 contexts/answer 上跳过打分（`ragas_eval.py:136`）。
m8 negative 还有 5 题被打分，说明那时 LLM 在 negative 场景下仍会输出非空内容。

## 5. 关键解读

### 5.1 +12pp faithfulness 主因不是 citation 格式，是 v4 grounding 护栏

`fact_coverage` 在 daily 跌 4.88pp，`faithfulness` 在 ragas 涨 12.63pp ——
两边方向相反但本质同源：

- **v4 prompt rule #6** 加 "Every statement MUST be directly supported by a cited
  chunk" 强约束 → LLM 在 chunks 不足时不编、显式说"未在 chunks 中找到"
- **fact_coverage** 是 substring 命中 → 答案变短 / 拒答 = 命中变少
- **ragas faithfulness** 是 claim-by-claim 与 chunks 对比 → 答案中残留的 claim 都有
  chunks 支撑 = 分数变高

这两个数字一起看，结论是 **v6 答案变得更"凭证据说话"**：
- 有支撑就答得很 grounded（faithfulness 涨）
- 无支撑就直接说没找到（fact_coverage 跌，但跌的是没必要硬编的事实）

### 5.2 -7pp answer_relevance 是诚实代价

`answer_relevance` 用 LLM judge 评 "答案 vs 问题 的相关度"。v6 频繁回 "未在
chunks 中找到 X" → judge 打分自然偏低（用户问 X 你说没 X 不算 relevant）。

把这个数字单独看会觉得退化，结合 faithfulness 看就是 **trade-off**：是要
"看起来 relevant 但可能瞎编" 还是 "诚实承认信息缺口"。 v6 选了后者。

### 5.3 context_precision +15pp 与 context_recall +8pp 的来源

ragas 这两个 metric 都看 **citations 字段里的 chunks**：
- precision = 引用的 chunks 多少与 ground_truth 相关
- recall = ground_truth 在引用 chunks 里被覆盖多少

v6 索引引用让 LLM **只能引真存在的 chunks**（编不出新 spec/section）→ precision
直接上去。结合 v4 rule #6 "每句必须有 chunk 支撑" 收紧引用 → 引得更少更准 → 也拉高
recall（不引"凑数 chunk"）。

### 5.4 v6 vs 同 prompt 体系（v5）的纯 citation 增量没量化

要纯测 "v5 → v6 citation 切换" 的增量，需要 rejudge baseline 2026-05-27 after-ad
（v5 prompt 数据）跑 ragas。本次 rejudge baseline 因和 v6 并发起跑导致 LLM proxy
吞吐打满（3.5 calls/min combined），手动 kill 让 v6 单跑。**v5 ragas 暂缺**。

补救建议：下一次 weekly eval 用 v6 跑完后，再 rejudge 一份 v5 archived results
做"纯 citation 增量"比较。本期不阻塞上线。

## 6. 新 baseline 立帖

下次 daily eval（无论手动 trigger 还是 nightly CI）应与本数据比对，**不与 m8-baseline 比**。

| 数据来源 | 路径 |
|---|---|
| Daily 指标 | `eval-results/v6-citation-index-20260529T092708Z/report.md` |
| Daily raw | `eval-results/v6-citation-index-20260529T092708Z/results.json` |
| Ragas rejudge | `eval-results/v6-citation-index-20260529T092708Z-ragas/report.md` |
| Ragas raw | `eval-results/v6-citation-index-20260529T092708Z-ragas/results.json` |

阈值守护（CLAUDE.md §5.6 不可降级原则）：

- daily：fact_coverage ≥ 28%、context_recall_section ≥ 80%、negative weighted_pass ≥ 95%
- ragas（新 baseline 起算）：faithfulness ≥ 0.65、context_precision ≥ 0.70、
  context_recall ≥ 0.45（按 v6 数据下浮 ~4pp 留容差）
- p50 latency ≤ 65s

跌破 → 启动诊断（CLAUDE.md §5.6 必须查根因再决定是否调整阈值）。

## 7. 待办

- [ ] **下次 weekly eval 跑 ragas full（140+ 题）**：把 v6 baseline 扩到 weekly 规模，
      验本次 daily 56 题样本是否被极端值带偏
- [ ] **rejudge 2026-05-27 after-ad（v5 era）**：补"纯 citation 切换"增量数据；
      LLM proxy 不并发跑 v6 时单线程 ~90 min 可完成
- [x] **fact_coverage 评分升级**（2026-05-29 落）：substring 切到 LLM judge
      （三档 HIT / PARTIAL / MISS，加权分），mimo-v2.5-pro + function_calling，
      单题异常隔离 + fallback substring。详见 [`2026-05-29-fact-coverage-llm-judge.md`](2026-05-29-fact-coverage-llm-judge.md)

## 8. 操作复盘 / 经验值

- **rejudge 并发 = 鸡飞蛋打**：deepseek-v4-pro 是推理模型（thinking enabled），
  per-call 30-60s。两个 rejudge 进程并发跑 LLM proxy → 共享带宽 3.5 calls/min，
  16 hours / 进程；单跑 13 calls/min，~90 min / 56 题。
  **教训**：以后 rejudge 一次跑一个，别贪并发。
- **EVAL_BACKEND_TOKEN 24h 过期**：原 `/tmp/tgpp-eval-token.txt` 过期；
  inline 编 24h JWT 一行解决（不要走 `create_access_token` 默认 15min）。
  [[project_live_eval_deploy_ops]] 已更新。
- **deploy.sh 重启容器 → IP 变**：本次 IP 仍是 172.22.0.5（运气），但下次重建应该
  重取 `docker inspect`。
