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

### #1 拒答词表扩展 ⚠️ 2026-05-20 已落，但同日被 #1b LLM-judge 替代

> 时间线：
> - 第一轮 commit 27052fb：扩了 8 zh + 8 en 短语 + must_nf 解耦 forbidden（#1 + #2）
> - 第二轮决议：substring 词表的根本缺陷（开放集 + 区分不出"X 存在"vs"X 不存在"）
>   靠扩词表无法根治，**彻底放弃 substring**，改用 LLM judge（详见 §3.1b）
> - `not_found_phrases.py` + 镜像保留供 `suggest_questions` 节点未来使用，不再是 eval metric 输入



backend / eval 两侧词表镜像新增 8 个 zh + 8 个 en 短语；mirror 校验测试同步加行；新增/修改测试全绿（eval 71 + backend 38）。

新增 zh：`前提不成立 / 无法回答 / 无法支持 / 无法确定 / 未提及 / 并未涉及 / 并不存在 / 未包含`
新增 en：`cannot answer / cannot support / cannot determine / premise does not hold / not mentioned / does not apply / does not exist / no information`

> 选词原则：**只收紧紧捆"规范未覆盖 / 用户假设不成立"语义的表达**；故意不收"不会"、"并非"、"不使用"这类太通用的，避免正答里普通否定句触发误判。

实现位置：
- `backend/app/agent/not_found_phrases.py`
- `eval/not_found_phrases.py`（镜像 + 测试守门）
- `eval/tests/unit/test_not_found_phrases.py::test_mirror_with_backend_module`

### #1b LLM-as-judge 替代 substring（负样本） ✅ 2026-05-20 已落

放弃 substring 思路，新增 `eval/negative_judge.py::NegativeJudge`：

- 输入：`(item, AgentResponse)`；judge LLM = glm-5.1（含 1e-8 → 0.01 修复）+ function_calling 拿三档枚举
- 输出：`{"verdict": "VALID_REFUSAL" | "PARTIAL_REFUSAL" | "INVALID" | None, "reason": "..."}`
- 单题异常隔离：LLM 调用失败 / 返回未知 verdict / 空 answer → verdict=None + reason 写明
- Prompt：zh / en 双语，明确"允许复述伪概念用以否认"以避免 forbidden-style 误判

`EvalResult.must_say_not_found_passed: bool` 字段移除，由
`negative_judge_verdict: str | None` + `negative_judge_reason: str | None` 替代。
`aggregate` 新增 `negative_judge.verdict_counts` + `valid_rate`；`write_report` 在
"Failed / Notable" 段对 PARTIAL/INVALID 题打 verdict + reason 便于排查；
Langfuse 上传 `negative_judge_score`（VALID=1 / PARTIAL=0.5 / INVALID=0；其它 skip）。

阈值（详见 `06-md §7`）：
- D13 宽松（M7 nightly）：weighted pass `(VALID + 0.5·PARTIAL) / total ≥ 0.85`
- D13 严格（M8 上线）：`VALID / total = 1.0`

涉及文件：`eval/negative_judge.py`（新）/ `eval/runner.py`（字段 + 调用点 + aggregate + report + langfuse mapper）/ `backend/tests/eval/test_golden_v1.py`（daily 断言换 verdict 口径）/ `eval/tests/unit/test_negative_judge.py`（新 31 单测）/ `eval/tests/unit/test_runner.py`（更新 must_nf 旧 case）。

### #2 must_nf 通过时放宽 forbidden ✅ 2026-05-20 已落（但被 #1b 替代后语义已消失）

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

## 5. LLM-judge 实跑结果（2026-05-20 二轮收尾，验证 judge 效果）

`eval-results/m7-negative-only/20260520T094653Z`：16 题 negative + glm-5.1 judge，
不跑 ragas，~13 分钟。

| verdict | count | item_ids |
|---|---|---|
| VALID_REFUSAL | 14 | hand-neg-001..008, 010, 012, 013, 014, 015, 016 |
| PARTIAL_REFUSAL | 1 | hand-neg-009 |
| INVALID | 1 | hand-neg-011 |

- `valid_rate` = 14/16 = **0.875**
- `weighted_pass_rate` = (14 + 0.5)/16 = **0.906** ≥ 宽松档阈值 0.85 ✅
- `forbidden_violation_rate` = 1.0（每条都命中 forbidden —— 但 judge 不再被骗）

**Judge 真信号**（两条 non-VALID 都是有意义的判定）：

- **hand-neg-009 PARTIAL_REFUSAL**：agent 答 "未直接提及 BAF... 现有资料主要描述 AF..."
  Judge reason：「Agent 仅弱化表述'未直接提及 BAF'，暗示 BAF 可能存在但检索未覆盖，
  随后用大量篇幅解释 AF/BDT 机制，容易误导用户认为 BAF 是真实概念只是资料未涉及。」
  这是真实的"软幻觉" —— agent 没明确否认伪概念，反而给了 BAF 一个"AF 的同义词"暗示
- **hand-neg-011 INVALID**：agent 答 "当 UE 处于 5GMM-HIBERNATE 状态时，AMF 不会立即
  发起分页..." Judge reason：「Agent 完全未拒答，将不存在的'5GMM-HIBERNATE 状态'
  当作真实概念，详细编造了 AMF 在该状态下的分页处理流程、eDRX 机制和规范引用。」
  这是真幻觉（伪状态 + 编造 spec 引用），属于 agent 必须修复的问题

**意义对照**：

| metric | 原 substring 口径 | LLM-judge 口径 |
|---|---|---|
| daily 通过 | 0/16（全失败）| 14.5/16 weighted = 0.906（pass）|
| 区分力 | 0（一刀切）| 区分出 1 真幻觉 + 1 软幻觉 + 14 合理拒答 |
| forbidden 偏差 | 致命（拒答必引用伪概念 → 全 fail）| 无（judge 在 prompt 里明确"允许复述伪概念以否认"）|
| 边界题处理 | neg-012/neg-014 误判（agent 答得合理但词表不命中）| 正确通过（judge 看语义）|

## 6. 后续 todo（更新）

- **#3 fact_coverage prompt 调优**：仍待办（属 agent 范畴）；下次完整 daily 重跑时
  一并验
- **#4 neg-012 / neg-014 题目复核**：可降级 —— 今天实跑里 LLM judge 已经把这两条都
  判定为 VALID_REFUSAL（"SST=99 不是标准化业务"和"EAP-WEP 不存在"都被识别为合理拒答），
  题目本身可能不需要改。剩下需要懂 3GPP 的人审的是 neg-011 这种 agent 真幻觉 ——
  到底是 agent 检索 / generate 哪个环节让 agent 把 "5GMM-HIBERNATE" 当真的，这是
  agent 调优范畴（更接近 #3 而不是题目 bug）
- **完整 daily**（含 ragas + 全 56 题）等 #3 落地后再跑，预期 ragas faithfulness +
  context_recall 都会显著上升（之前 0.18 / 0.018 主要是 judge 不工作的 NaN 拉低）
