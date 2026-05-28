# 2026-05-28 — Prompt A/B 实验:5 种 generate prompt 思路对比

## 背景

`generate_qa` prompt v3→v4 加 grounding 护栏后,definition recall 1.0 + faithfulness 部分
回收,但仍有掉点空间(2026-05-27 findings)。本次跑受控实验,看其他 prompt 思路能否更好。

## 方法

**控制变量**:同 10 道 hand_crafted 题、同检索上下文(每题用 `call_agent` 打 live backend
抓 reranked chunks 一次,固定)、同生成模型(`mimo-v2.5-pro`)、同温度 0.1。**只换 prompt**。

**5 个 prompt(v4 当前 + 4 个新思路)**:全部共享相同的 hard rules(no fabrication、引用
格式 `[spec_id §section_path]`、verbatim IE 名、LaTeX、语言)和"先 1-3 句直接答→bullet
+ 引用"输出结构。只换 rule #6 的"思路"段:

### v4_current(当前部署 — 基准)
完整 + grounding 护栏:"完整回答所问、每句须有 chunk 支撑、禁堆砌问题没问的 edge case"。

### P1_extractive(抽取式 / 贴原文)
> Answer using ONLY information explicitly stated in the chunks. Prefer quoting or
> closely paraphrasing the chunk wording; do not synthesize, generalize, or add
> background beyond the chunks. Every sentence must be directly traceable to a
> specific chunk. Lead with the definition/answer exactly as the chunk states it.

### P2_structured(结构化模板)
> Use EXACTLY these sections, nothing more:
> **核心定义/直接答案**:1-2 句直接回答问题。
> **关键要素**:chunks 明确给出的关键字段/参数/步骤,逐条引用。
> **适用范围/条件**:仅与问题直接相关的条件(无则省略本节)。
> Fill from the chunks, then STOP. Do NOT add extra sections, tangential edge cases,
> or release-specific enumerations.

### P3_scope_tight(紧扣所问 / 拒绝铺陈)
> First, in ONE short line, restate what the question asks for.
> Then answer EXACTLY that and nothing the question did not ask. If asked "what is
> X", define X and its directly-relevant attributes, then stop.
> Actively resist listing tangential sub-cases, conditions, or release-specific
> details not needed to answer the question.

### P4_self_verify(草稿 + 自检)
> Internally draft a complete answer, then re-check each sentence against the chunks
> and DROP any sentence not directly supported by a cited chunk. Be complete on what
> the question asks, but every sentence in the FINAL output must be grounded in a
> cited chunk. Output ONLY the verified final answer (do not show drafting/checking).

**10 道题**(hand_crafted、非 negative): hand-def-002 / def-006 / def-007 / proc-001 /
proc-004 / formula-001 / formula-003 / multi-004 / table-002 / table-005。

**打分**:ragas faithfulness + answer_relevance(judge=deepseek-v4-pro,RunConfig
timeout=900s、max_workers=2)+ 子串 fact_coverage + 答案长度。

**Harness 脚本**:`eval/scripts/prompt_ab_experiment.py`(抗超时 3 次重试、增量落盘、
断点续跑)。原始 rows.json 在 `eval-results/2026-05-28-prompt-ab/`(gitignore,不入库)。

## 样本说明:50 行里有效 35 行(n=7 严格可比)

- **`hand-def-006`**:这次 call_agent 拿回的 chunks_rerank 为空 → ragas 全 skip,该题
  全 prompt 剔除。
- **`hand-def-007`、`hand-proc-004`**:各有 2 / 2 个 prompt 生成阶段超时(len=0,3 次
  重试都失败),严格可比受影响 → 剔除该题。
- 剩 **7 题 × 5 prompt = 35 有效行**用于严格 apples-to-apples 比较。
- 生成失败统计:v4=0、P1=2、P2=2、P3=1、P4=0。**P1/P2 生成时长偏大可能触发 LLM 端
  超时**。

## 整体均值(n=7 严格)

| prompt | faith | ans_rel | fact_cov | avg_len | 生成失败 |
|--------|------|---------|----------|---------|---------|
| v4_current | 0.708 | 0.799 | 0.224 | 551 | 0 |
| P1_extractive | 0.786 | 0.802 | 0.219 | 417 | 2 |
| P2_structured | 0.740 | **0.846** | **0.348** | 551 | 2 |
| P3_scope_tight | 0.556 | 0.735 | **0.367** | 420 | 1 |
| **P4_self_verify** | **0.813** | 0.817 | 0.200 | **314** | 0 |

## 各类别 faith

| 类别 | v4 | P1 抽取 | P2 结构 | P3 紧扣 | P4 自检 |
|------|----|--------|--------|--------|--------|
| definition | 0.8 | **1.0** | 0.82 | **1.0** | 0.9 |
| procedure | **1.0** | 0.5 | 0.75 | **0.14** ⚠️ | 0.63 |
| formula | 0.47 | 0.75 | 0.71 | 0.53 | **1.0** |
| multi_section | 0.71 | **1.0** | 0.78 | 0.36 | 0.67 |
| table_lookup | 0.75 | 0.75 | 0.71 | 0.67 | 0.75 |

## 逐题"谁第一"统计(faith / ans_rel / fact_cov)

| prompt | faith#1 | ans_rel#1 | fact#1 |
|--------|---------|-----------|--------|
| v4_current | 2 | 0 | 3 |
| P1_extractive | 4 | 1 | 4 |
| P2_structured | 2 | 1 | **6** |
| P3_scope_tight | 2 | **4** | 5 |
| P4_self_verify | 3 | 1 | 3 |

## 结论:**无单一 prompt 全面碾压**,各有强弱

- **P4 自检**:整体 faith 最高(0.813,vs v4 +0.10)、答案最短(314,vs v4 −43%)、
  生成无失败。**fact_cov 垫底 0.2**——可能过度收敛漏 facts。"质量最稳但偏保守"。
- **P2 结构化**:**ans_rel 全场最高**(0.846)+ **fact_cov 接近最高**(0.348)+ faith
  不输 v4。**最均衡**,但 2 格生成失败。
- **v4 现状**:整体中等,但 **procedure 1.0 独一档**——"完整覆盖正常论述"对流程题最自然。
- **P1 抽取式**:definition + multi_section 双 1.0,但 procedure 0.5、fact_cov 最低。
  适合定义题,不适合流程。
- **P3 紧扣所问**:**procedure 灾难(0.143)** ——"只答所问"把流程题剪太狠。**不推荐**。

## 决定:暂不动 prompt

n=7、单次跑、judge 方差等,**数据不足以否定 v4 上线决策**。且若换 P4 拿 faith,会丢
fact_cov;换 P2 拿 ans_rel/fact_cov,faith 又略低 v4 一档;v4 在 procedure 上没人能比。

**保留实验结论作为后续迭代的输入**,这一轮不动 prompt。

## 后续可能的尝试(未做)

1. **v5 融合**:`v4 完整性 + P2 结构化分段 + P4 输出前自检`,在掉得最狠的 formula 题
   (v4 faith 0.47)上看能不能起来,不伤 procedure。
2. **按 query_class 分 prompt**:procedure 用 v4 思路,definition / formula 用 P4 自检
   思路。需在 `generate_node` 加分支或 prompt 模板按类选择。
3. **更大样本复测**:本轮 n=7,样本太小不足以下决定;若要正式换 prompt,至少 30 题
   跨类别复测,且最好两次独立 run 取均值,降低 judge 方差。

## 仍开口

- P1/P2 各 2 格生成超时:这两 prompt 措辞偏长(P2 含中文结构指令),可能让 prompt 总长
  接近 LLM 上限或拖慢生成。换上线前要排查。
- def-006 这次 chunks_rerank=0(上次跑同题有 8 个 chunk):retrieval 出空料的偶发现象,
  与 prompt 无关,但值得 ops 留意。
