# 2026-05-20 Daily Live Ragas 复盘 + 后续 todo

> 第一次 daily live ragas 在 `eval-results/m7-daily/20260520T063955Z`。
> 56 题 source==hand_crafted 全跑通，agent 端 56/56 `final`。
> 本文记录复盘结论 + 已落 / 未落项的明细。
>
> 上游报告：[`../03-development/06-evaluation-and-observability.md §0 M7.1`](../03-development/06-evaluation-and-observability.md)

## 1. 首跑聚合（D13 宽松档对照）

| 指标 | 实测 | 宽松阈值 | |
|---|---|---|---|
| context_recall_spec | 0.85 | — | ✅ |
| context_recall_section | 0.775 | ≥ 0.65 | ✅ |
| must_say_not_found pass | 0 / 16 | 100% | ❌ |
| forbidden_violation_rate | 42.9% | — | ⚠️ |
| fact_coverage | 0.31 | — | ⚠️ |
| ragas.faithfulness | 0.18 | ≥ 0.75 | ❌ |
| ragas.answer_relevance | 0.69 | ≥ 0.70 | ⚠️ |
| ragas.context_recall | 0.018 | ≥ 0.65 | ❌ |
| ragas.context_precision | 0.033 | — | ❌ |
| agent p50 latency | 39.0s | ≤ 6s | ❌ |

> ragas 三项极低主要是 judge 用了 qwen-max（glm-5.1 当时被 LiteLLM gateway 报 400）+ 大量
> `Job[N]: TimeoutError` / `OutputParserException`，分数可信度差。glm 修复后须重跑。

## 2. negative 0/16 的真正原因（分桶）

| 桶 | 数 | 含义 |
|---|---|---|
| both_pass | 0 | 同时通过 not_found 短语 + forbidden 干净 |
| 短语命中但 forbidden 撞 | 5 | 词表勉强命中（"未提及"/"未涉及"），但 forbidden 仍撞 |
| 短语没命中 + forbidden 撞 | 11 | 两条都失败 |

**两个独立的 metric bug，必须同时修才能恢复 pass**：

### 2.1 拒答短语词表覆盖太窄（16/16 都触发）

agent 实际拒答用语（从 16 条答案抽取）：

```
前提不成立 / 并非 / 不会 / 无法回答 / 无法支持 / 无法确定
并未提及 / 未提及 / 不存在 / 没有得到任何支持 / 并未涉及
```

原 `not_found_phrases.py` 词表只有 6 个偏正式短语（`未找到 / 未定义 / 规范未规定 / 不涉及 / 不在范围内 / 没有相关规定`），与 LLM 自由生成的拒答表达对不上。

### 2.2 forbidden 用 substring 撞拒答里的引用（16/16 都触发）

每条 negative 拒答都要**复述用户提出的伪概念**才能否认它，substring 匹配区分不出"X 存在"和"X 不存在"两种语境：

| 题 | 拒答原话 | forbidden 命中 |
|---|---|---|
| neg-001 | "5G基站的IPv6地址**并非**通过 PSS/SSS 编码" | `IPv6` |
| neg-004 | "**DCI format 5_0 不存在**" | `DCI format 5_0` |
| neg-005 | "UE **不使用** MAC 地址来推导 DM-RS" | `MAC地址` |
| neg-006 | "**无法确定** Type-4 HARQ-ACK codebook 决定天线端口" | `Type-4`、`天线端口` |

是 metric 设计 bug，不是 agent 幻觉。

## 3. 修法清单与状态

### #1 拒答词表扩展 ✅ 2026-05-20 已落

backend / eval 两侧词表镜像新增 8 个 zh + 8 个 en 短语；mirror 校验测试同步加行；新增/修改测试全绿（eval 71 + backend 38）。

新增 zh：`前提不成立 / 无法回答 / 无法支持 / 无法确定 / 未提及 / 并未涉及 / 并不存在 / 未包含`
新增 en：`cannot answer / cannot support / cannot determine / premise does not hold / not mentioned / does not apply / does not exist / no information`

> 选词原则：**只收紧紧捆"规范未覆盖 / 用户假设不成立"语义的表达**；故意不收"不会"、"并非"、"不使用"这类太通用的，避免正答里普通否定句触发误判。

实现位置：
- `backend/app/agent/not_found_phrases.py`
- `eval/not_found_phrases.py`（镜像 + 测试守门）
- `eval/tests/unit/test_not_found_phrases.py::test_mirror_with_backend_module`

### #2 must_nf 通过时放宽 forbidden ✅ 2026-05-20 已落

`compute_eval_metrics` 把
`must_say_not_found_passed = is_not_found_answer(answer, lang) AND not forbidden_violations`
改成
`must_say_not_found_passed = is_not_found_answer(answer, lang)`

`forbidden_violations` 字段不变，仍独立上报。

理由：forbidden 是为了抓 **正答里的幻觉关键词**；负样本的拒答必然要复述假设的概念，把 forbidden 当 must_nf 必要条件 → 系统性 false-negative。

实现：`eval/runner.py::compute_eval_metrics` + `eval/tests/unit/test_runner.py` 改 case + `docs/03-development/06-evaluation-and-observability.md §4.1` 同步。

### #3 fact_coverage 在表格 / 公式 题上偏低 ⏳ TODO（agent 范畴）

**现象**（非 negative 40 题 fact_coverage 0.31）：

| category | 题数 | fact_coverage 中位 | 典型问题 |
|---|---|---|---|
| table_lookup | 8 | 0.5 | retrieval section 正确但 agent 没把具体数值（"8" "6" "12" "16" "19"）摘到答案里 |
| formula | 8 | 0.0 (5/8 全 0) | retrieval section 正确但公式字面没摘出或被改写 |
| procedure / multi_section | 16 | 0.16 - 0.5 | 答了大方向，漏 expected_facts 里的具体子步骤 |

**根因猜测**：generate prompt 没强约束"涉及表格 / 公式时必须照原文摘抄字段名 / 数值 / 公式字面"。

**计划**：
1. 抓 3-5 条最典型的低分例（hand-table-002 / hand-formula-001/002/005 / hand-proc-003）人审 agent 实际答案 + 检索 chunk，确认是不是 prompt 问题
2. 在 `backend/app/agent/generate.py` 的 system / few-shot 里加"表格值 / 公式 / 字段名要原文摘抄不要改写"约束
3. retrieval 已经达标，本项**不要再去动 retrieve / rerank 节点**（属于 M7.5 batch C 范围）
4. 验收：daily 重跑 fact_coverage ≥ 0.5（非 negative 题），table_lookup / formula 中位 ≥ 0.6

**优先级**：中。在 #1 #2 修完后跑出干净 baseline 看 ragas faithfulness 还低不低再决定。如果 ragas faithfulness 也升了说明 generate 整体 OK，本项可降优。

**owner**：vibe coding 模式下 Agent 自驱；本任务跨 backend/agent + eval 两侧。

### #4 题目复核（neg-012 / neg-014 边界） ⏳ TODO（human review）

| 题 | 议题 | 建议 |
|---|---|---|
| hand-neg-012 | 题目假设 SST=99 不存在；3GPP TS 23.501 §5.15.2 实际把 SST 95-127 标为 reserved-non-standardized，agent 答"SST 99 不是为特定业务设计的标准化切片"算正确陈述，不算拒答 | 改题为更明确的"不存在的 SST"或干脆删掉 |
| hand-neg-014 | EAP-WEP 确实不存在于 5G primary auth；agent 答"不支持"是正确拒答；用 #1 扩词表的 `无法回答 / 不存在` 应能 cover | 暂保留，等 #1 修完看 daily 是否仍 fail；若仍 fail 则改题 |

**计划**：
1. 等 #1 #2 修完后跑一次 daily，看 negative 16 题里实际剩多少 fail
2. 对剩下的 fail 题做"agent 答错 vs 题目本身有 bug"二分；后者归人审待办
3. 人审建议交懂 3GPP 的人（vibe coding 模式下需要外部介入，详见 `00-vibe-coding-protocol.md §5.5`）

**计入**：[`06-evaluation-and-observability.md §12 M7.0`](../03-development/06-evaluation-and-observability.md) 已有 `[human]` "至少 20 题（手写部分）由懂 3GPP 的人 review 过" 待办；本项是该 todo 的子集，应在那次 review 里一并处理。

## 4. 其他次要发现

### 4.1 latency p50 39s vs 阈值 6s

agent 全链路慢。属于已知问题，[`06-md §0 M7.5 batch C`](../03-development/06-evaluation-and-observability.md) 的 retrieval 校准 + `test_retrieve_node_p50_latency_under_800ms` 处理范围，不在本复盘 todo 里。

### 4.2 ragas judge 走 LiteLLM 时 GLM 400

已修：`fix(eval): GLM judge 温度被 ragas 覆盖成 1e-8 → 400 → 4 metric 全 NaN`（22a6c25，2026-05-20）。判定细节见 [`06-md §12 M7.2`](../03-development/06-evaluation-and-observability.md) 的"2026-05-20 GLM 温度修法"段落。

### 4.3 临时脚本 `/tmp/run_daily_live_ragas.py` 用完未销毁

非长期资产；下次需要 daily 命令行入口时再决定是 CLI 化（`eval rag run --daily --ragas`）还是直接走 `make eval-daily` + `RUN_LIVE_EVAL=1`（后者目前不生成 report.md / results.json）。本期不动。

## 5. 重跑触发条件

#1 #2 已落，可以重跑一次 daily 拿干净的 baseline。但**建议先不跑**，理由：

- ragas judge glm-5.1 修了，词表 + must_nf 修了，但 #3 fact_coverage 没动 —— 跑一次只验证 negative 题是否恢复 pass，其他指标变化预期都不大
- 跑一次成本 ~2 小时 + 数百次 LLM 调用，最好攒到几个改动一起验
- 等 #3 prompt 调优落地后再跑，能一次看清三件事的合力效果

如果只是想验 negative 恢复，可以加 `--source-filter=hand_crafted --category-filter=negative` + ragas 关闭跑一遍（~5 分钟），但目前 runner 不支持 category 过滤，需要先加。本期不动。
