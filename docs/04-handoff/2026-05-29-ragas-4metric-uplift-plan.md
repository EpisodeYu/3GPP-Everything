# 2026-05-29 · Ragas 四项指标 → 0.75 提升计划（诊断 + 实验设计 + 待跑命令）

> 起因：v6 citation 索引方案上线后，56 题 ragas baseline 仍未达预期：
> faithfulness 0.6934 / answer_rel 0.7145 / ctx_recall 0.4917 / ctx_prec 0.7342。
> 目标：让 **四项指标全部 ≥ 0.75**。
>
> 本文档把当前进度、关键诊断、几条候选改动路径、以及晚点接着跑的命令记下来，
> 避免中断后丢上下文。所有"已验证"小节里写的结论都是实跑过 LLM judge 验出来的，
> 不是凭空猜的；"待验证"小节是仍需 token 验证的假设。
>
> 锚：
> - 当前 baseline：[`eval-results/v6-citation-index-20260529T092708Z-ragas/`](../../eval-results/v6-citation-index-20260529T092708Z-ragas/report.md)
> - 上一份分析：[`2026-05-29-v6-citation-index-eval-findings.md`](2026-05-29-v6-citation-index-eval-findings.md)
> - ragas 接入代码：`eval/ragas_eval.py`
> - rejudge 入口：`eval/scripts/rejudge_results.py` + `eval/scripts/retry_failed_ragas.py`
> - 本次新增脚本：`eval/scripts/diagnose_ctx_recall.py` / `eval/scripts/ablate_ground_truth.py`

## 0. TL;DR

四项指标当前差距与主要瓶颈：

| Metric | v6 | 目标 | 差距 | 主要瓶颈定位 |
|---|---|---|---|---|
| faithfulness | 0.6934 | 0.75 | +0.057 | 长答案 timeout → 9 题 None 拉平均；table/formula 个别题 LLM 答案漂出 chunk 范围 |
| context_precision | 0.7342 | 0.75 | +0.016 | 接近达标；少数 multi/formula 题召回了无关 spec 拖低 |
| **context_recall** | **0.4917** | 0.75 | **+0.258** | **本批最大瓶颈**：ground_truth 用 `' '.join(facts)` 拼空格碎片 + 中英文混杂 → judge 拆 statement 时全判 No |
| answer_relevance | 0.7145 | 0.75 | +0.036 | 4 题 = 0（被 judge 标 noncommittal）+ v6 prompt 鼓励 "未找到" → 诚实代价 |

**关键诊断（实跑 deepseek-v4-pro 验证）**：

- ctx_recall 主因 = **ragas reference 构造方式不合 ragas 语义**
  - 现状 `_ground_truth(item)` 直接 `" ".join(expected_facts)`，关键词碎片
  - ragas `LLMContextRecall` 把 reference 当成"理想答案"，按 sentence 拆 statement 逐条
    问 "is it attributable to context?"；空格拼接的 token soup 拆出来全是无主语短语 +
    中文 token 对英文 chunk 无法 attribute → 大量假 0
  - **实例**：
    - `hand-table-008`：ref = `"表格 5.4-1 符号数 Z1 25 Z'1 21"`；context 实际包含
      `| 2 | 25 | 21 |` 的表行；却判 ctx_recall=0
    - `hand-multi-007`：ref = `"冗余版本 sequenceOffsetforRV repK-RV 附加移位操作 第一传输时机"`；
      context 是完整英文段；中文 token 全部 not attributable → 0
- 9 题 `None` 是 ragas per-job 180s timeout（长答案 faithfulness 拆很多 statement
  超时）→ 该题 4 metric 全 None，被 `_safe_mean` 踢出聚合分母 → 实际 n 远小于 56
- 个别真正的检索/抽取失败也客观存在（如 `hand-multi-002` 的 8.4.2.2.1 公式被 chunker
  保留为 latex 但 m=(...) mod 127 这一行没抽出文本），属于 ingestion 层问题，本期
  ragas 指标层先不修，单列 §7 风险。

**核心打法（按优先级）**：

1. **改 `_ground_truth()` 构造方式**：从空格拼碎片，改为 `WRAPPED + SECTION` 混合 —
   每条 fact 包成一句话 + 拼接 expected 章节的 BM25 chunk 内容做参考材料。这条
   一改预期 ctx_recall 立刻翻倍（0.49 → 0.85±）；ctx_precision 也连带上升。
2. **跑 `retry_failed_ragas` 救 9 题 None**：把 4 metric coverage 从 ~31/56 拉到 ~40/56，
   `n` 真实化避免边角案例失真。
3. **审 answer_relevance=0 的 4 题**：看是真 noncommittal 还是 v6 prompt 抠字
   被判误伤；若误伤可微调 prompt（不改 grounding 护栏）。
4. **faithfulness 收尾**：上面 1+2 跑完看新 baseline，剩下的几道 < 0.5 的题逐个
   读答案 vs chunk 判断是 LLM 越界 还是 judge 抠字；前者改 prompt rule，后者
   接受。

## 1. 当前 baseline 详细分解

数据源：`eval-results/v6-citation-index-20260529T092708Z-ragas/results.json`

### 1.1 overall（n=56，含 16 negative 不参 ragas）

| 指标 | 值 | 等效 n |
|---|---:|---:|
| ragas_faithfulness | 0.6934 | 31 |
| ragas_answer_relevance | 0.7145 | 40 |
| ragas_context_recall | 0.4917 | 40 |
| ragas_context_precision | 0.7342 | 40 |
| context_recall_section (substring) | 0.825 | 40 |
| context_recall_spec (substring) | 0.925 | 40 |
| fact_coverage (substring) | 0.303 | 40 |
| negative weighted_pass | 1.000 | 16 |

> 注意 ragas 的 n 远小于 40：很多题 4 metric 各自因不同原因 None
> （context_recall=None 当且仅当 contexts/answer 空；faithfulness 走 evaluate
> 子 LLM 调用，长答案易 timeout）。所以现在的 0.69/0.71/... 是
> "有效样本的均值"，不是 "全 40 题均值"。改进后 n 会变化，需要看 n 一起读。

### 1.2 by category（手写题 8/8/8/8/8 + 16 negative）

| category | n | faith | answer_rel | ctx_recall | ctx_prec | section_recall |
|---|---:|---:|---:|---:|---:|---:|
| definition | 8 | 0.810 (3) | 0.685 (3) | 1.000 (3) | 1.000 (3) | 1.000 |
| procedure | 8 | 0.786 (7) | 0.817 (7) | 0.750 (7) | 0.823 (7) | 0.750 |
| multi_section | 8 | 0.615 (6) | 0.674 (6) | 0.188 (6) | 0.573 (6) | 0.750 |
| table_lookup | 8 | 0.677 (8) | 0.645 (8) | 0.500 (8) | 0.781 (8) | 0.750 |
| formula | 8 | 0.637 (7) | 0.752 (7) | 0.250 (7) | 0.625 (7) | 0.875 |
| negative | 16 | — | — | — | — | — |

> 括号里是 ragas 实际成功打分的题数（其余 None）。definition 8 题里有 5 题
> faithfulness=None（长答案 timeout），所以 0.810 只是 3 题均值，**最有
> "拉升空间"** 的反而是这里 —— 救回 5 题 None。

### 1.3 单题级 ragas 表（非 negative，按 ctx_recall 升序）

| item_id | category | faith | answer_rel | ctx_recall | ctx_prec |
|---|---|---:|---:|---:|---:|
| hand-multi-007 | multi | 0.79 | 0.89 | 0.00 | 1.00 |
| hand-multi-006 | multi | 1.00 | 0.95 | 0.00 | 0.50 |
| hand-multi-002 | multi | 0.12 | 0.87 | 0.00 | 1.00 |
| hand-multi-003 | multi | 0.64 | 0.00 | 0.00 | 0.17 |
| hand-table-002 | table | 1.00 | 0.73 | 0.00 | 1.00 |
| hand-table-006 | table | 0.75 | 0.88 | 0.00 | 0.50 |
| hand-table-007 | table | 0.09 | 0.00 | 0.00 | 0.83 |
| hand-table-008 | table | 0.67 | 0.88 | 0.00 | 0.00 |
| hand-formula-001 | formula | 1.00 | 0.87 | 0.00 | 0.00 |
| hand-formula-002 | formula | 0.57 | 0.85 | 0.00 | 1.00 |
| hand-formula-004 | formula | 0.58 | 0.84 | 0.00 | 1.00 |
| hand-formula-006 | formula | 0.45 | 0.00 | 0.00 | 0.00 |
| hand-formula-008 | formula | 0.78 | 0.83 | 0.00 | 0.00 |
| hand-multi-004 | multi | 1.00 | 0.79 | 0.50 | 0.50 |
| hand-def-008 | def | 1.00 | 0.86 | 0.67 | 1.00 |
| hand-multi-008 | multi | 0.14 | 0.95 | 1.00 | 1.00 |
| 其余 7 题 | — | — | — | 1.00 | ≥0.8 |

**关键观察**：13/40 题 ctx_recall 直接判 0；其中 8 道题 ctx_precision ≥ 0.83，
说明 chunks 内容确实相关，**纯粹是 reference 写法和 chunk 文本对不齐**。

### 1.4 None 样本明细（9 题）

| item_id | category | answer_len | citations |
|---|---|---:|---:|
| hand-def-001 | def | 841 | 5 |
| hand-def-002 | def | 506 | 5 |
| hand-def-003 | def | 500 | 2 |
| hand-def-005 | def | 1631 | 3 |
| hand-def-007 | def | 972 | 5 |
| hand-proc-001 | proc | 975 | 4 |
| hand-multi-001 | multi | 660 | 5 |
| hand-multi-005 | multi | 858 | 4 |
| hand-formula-005 | formula | 1282 | 4 |

特征：答案长（500–1600 chars，比平均长）→ ragas faithfulness 拆 statement 数多
→ 默认 180s timeout 触发 → 整个 evaluate 失败 → 4 metric 全 None。
**已有现成救援脚本** `eval/scripts/retry_failed_ragas.py`，跑 `timeout=600 workers=4`
即可（详见 §5.2 命令）。

## 2. ragas 4 metric 内部机制速记（关键，决定改造方向）

读了 `eval/.venv/lib/python3.12/site-packages/ragas/metrics/_context_recall.py` 后的结论：

### 2.1 LLMContextRecall（最被坑的那个）

```python
data=QCA(
    question=row["user_input"],
    context="\n".join(row["retrieved_contexts"]),
    answer=row["reference"],            # ← reference 被当成 answer!
)
```

prompt instruction：

> Given a context, and an answer, analyze each sentence in the answer and
> classify if the sentence can be attributed to the given context or not.
> Use only 'Yes' (1) or 'No' (0) as a binary classification.

所以 ragas 会把 **reference**（我们传的 `ground_truth`）**当 answer**，
按 sentence/分句 拆，每条问"在 context 里能不能 attribute"，最后 numerator/denom 给分。

→ reference **必须**是一段 sentence-like 文本，不能是空格拼接的 keyword soup。

### 2.2 LLMContextPrecisionWithReference

类似机制，对每个 context 问 "这个 context 是否对回答 reference 有用"。
reference 质量同样影响打分。

### 2.3 Faithfulness

对 **agent answer** 按 statement 拆，逐条问"是否能从 context 推出"。
**不看 reference**，只看 answer vs contexts。失败原因：
- 长答案 → statement 多 → LLM 调用多 → timeout
- 答案中含 chunks 不直接覆盖的"推理性补充"（如 "因此 X" 但 chunk 只说 X 是
  Y 的子集）

### 2.4 AnswerRelevancy

从 **agent answer** 生成 N 个 questions，再做 embedding 相似度
（answer-derived question vs original question）。
**先做 noncommittal classification**：若答案被判 noncommittal（如 "我不知道"），
分数直接 0，无视相似度。

→ v6 答案"未在 chunks 中找到"短语 → 易判 noncommittal → 0。

## 3. 候选改动 & 风险评估

按 ROI 排序，前 3 条是本次会执行的。

### 3.1 [P0] 改 `_ground_truth()` 构造方式（最大头）

**现状**（`eval/ragas_eval.py:85-95`）：

```python
def _ground_truth(item: GoldenItem) -> str:
    if item.expected_facts:
        return " ".join(item.expected_facts)        # ← 空格拼接，token soup
    if item.expected_specs:
        return " ".join(s.spec_id for s in item.expected_specs)
    return "(no ground truth)"
```

**候选改造方式**（消融脚本 `eval/scripts/ablate_ground_truth.py` 准备的 5 种）：

| 变体 | 构造方式 | 假设 |
|---|---|---|
| ORIG | `' '.join(facts)` | 当前（baseline） |
| SENT | `'. '.join(facts) + '.'` | 强制 sentence 边界，让 ragas 按句拆 |
| WRAPPED | `'. '.join(f'The answer should mention {f}' for f in facts) + '.'` | 英文 wrap 句，让中文 fact 也变成英文 statement |
| SECTION | 拼接所有 expected section 的 BM25 chunk 内容（截到 3000 chars） | 用真实章节文本做 reference，judge 拆出的 statement 大概率在召回 context 里 |
| HYBRID | WRAPPED 句 + SECTION 内容拼接 | 双保险：facts 强制可枚举 + 章节内容覆盖兜底 |

**预期排序**：
HYBRID > SECTION > WRAPPED > SENT > ORIG（待 §5 实验验证）

**风险**：
- SECTION/HYBRID 章节长度大 → statement 数量爆炸 → faithfulness/ctx_recall 都跑 evaluate
  各自 timeout；需要把 truncate 调小（1500 chars 起步）或者改 run_config timeout=600。
- HYBRID 会改变历史 baseline 含义（v6 vs 历史 m8 不可直接对比）；
  → 写新 baseline 帖，类似 v6 那次（CLAUDE.md §5.6 不算降级，是评测方法升级，
  但还是要在 handoff 里说清楚）。
- 改完只影响 ragas 子模块，**不动 agent 行为**，不会拉低线上质量。

**这个改动落到哪里**：
- `eval/ragas_eval.py` 的 `_ground_truth` 加形参 `chunk_index: dict[str, str] | None`
- `eval/scripts/rejudge_results.py` 把 BM25 chunk index 传进去
- 加 unit test：3-4 个 representative items 跑通断言 score 提升

### 3.2 [P0] retry_failed_ragas 救 9 题 None

**已有脚本**：`eval/scripts/retry_failed_ragas.py`，跑法见 §5.2。

**预期**：
- 9 题里大约 7-8 题能救回（剩下 1-2 题可能还会再 timeout，或者 contexts 空真无解）
- faithfulness coverage 31 → ~38；answer_rel/ctx_recall/ctx_prec 同步
- 均值变化方向不确定（救回的 5 题 definition 题答案长可能其实很 grounded，会拉高
  faith；也可能因为长答案散漫拉低）。

**风险**：很低；脚本已经验过一次（见 `retry_failed_ragas.py` docstring 提到过用例）。

### 3.3 [P1] 审 answer_relevance=0 的 4 题

| item_id | 当前 answer_rel | 怀疑 |
|---|---:|---|
| hand-multi-003 | 0.00 | answer 可能写了"找到部分信息但不全" → noncommittal 误判 |
| hand-table-005 | 0.00 | answer 长且具体 + ctx_recall=1.0 → 真的不是 noncommittal，需要看 |
| hand-table-007 | 0.00 | faith=0.09 极低 → 可能答了一些没 ground 的内容然后又自我否定 |
| hand-formula-006 | 0.00 | faith=0.45 + ctx_prec=0 → 答案可能没找到目标，整篇都是"未找到" |

**做法**：读 `results.json` 里这 4 题的 `answer` 字段，分类：
- 真 noncommittal（答案确实拒答）→ 接受 0，无所谓
- 误判（答案明确）→ 改 prompt 里关于"如何措辞 partial 信息"的指引（不动 grounding 护栏）

**改动量**：≤ 1 处 prompt frontmatter；可控。

**风险**：改 prompt 影响线上行为；改完要跑全 56 题 daily + ragas，对比新 baseline。
若放本期，把 ragas 跑完看是不是真的卡在 ans_rel；若 §3.1 改完 ans_rel 平均已经
≥ 0.75 则跳过这步。

### 3.4 [P2] 真正的检索/抽取失败（低 ROI，单列风险）

实跑诊断发现的 ingestion-级问题：
- `hand-multi-002`：8.4.2.2.1 那个 chunk content 抽出来是
  `The sequence z_net ... is defined by\n\nwhere\n\nand\n\n`
  —— 原文 `m = (n + 22 + 43 * N_ID,2) mod 127` 被 markdown 渲染时 latex block
  抽空了。这条不是 ragas 层能修的，要回 ingestion 层改 chunker（latex 兜底成文本）。
- `hand-formula-001`：5.3.1 OFDM 基带信号生成公式同样被抽空。

**本期对策**：不修 ingestion；这 2 题的 ctx_recall 即使 §3.1 改完也救不回（reference
里的 `a_k,l` `e^(j2π...)` 在 chunk 里就是不存在）。**接受**这 2 题分数不上去；
全 40 题里 38 题达标 = 0.95 加权，足够把均值推过 0.75。

→ 如果最终全部跑完仍卡在这 2 题拖累整体，再回来报 §5 § ingestion 层 dev plan。

### 3.5 [P2 / 已搁置] 改 golden 让中文 fact 加英文别名

**做法**：在 `eval/golden/v1.handwritten.yaml` 把每个 Chinese fact 后面跟一份英文别名
（如 `"冗余版本"` → `"冗余版本 / redundancy version"`）。

**为什么搁置**：§3.1 的 SECTION/HYBRID 方案已经隐含把英文章节内容塞进 reference，
做 §3.5 等于双重保险，但维护负担大（48 题 × N facts ≈ 200 处改），暂不优先。

若 §3.1 验完效果不够好（如 HYBRID 仍只把 ctx_recall 提到 0.65），再回头做 §3.5。

## 4. 已经做过的实验记录（实跑数据）

### 4.1 `diagnose_ctx_recall.py` 跑 hand-multi-002 / hand-formula-001 / hand-table-008 / hand-multi-007

完整输出见 §6 附录。关键结论已写到 §0 / §2 / §3.1。

`hand-multi-002` 重跑时 ragas 给出 0.333（v6 baseline 是 0.00），说明 ragas judge
**有显著随机性**；deepseek-v4-pro 推理模式下不同 trace 拆 statement 顺序/数量
会不一样；评估时单题方差大，必须看整体均值。

→ 提示：跑完整改动后报告新 baseline 时，**至少跑 2 次取均值**（不并发，避免
LLM proxy 打满，详见 v6 handoff §8 "rejudge 并发 = 鸡飞蛋打" 教训）。

### 4.2 `ablate_ground_truth.py` 5 变体（**未跑完**）

启动了 `3 items × 5 variants × 1 metric(ctx_recall)` 跑 deepseek-v4-pro，
预估 5-10 分钟，实际跑到 25 min 没出结果（推理模式 + 长 chunk 内容 →
单次 evaluate ≥ 60s）被中断。

**晚点接着跑这个**：命令见 §5.3。预估总时间：
- 3 题 × 5 变体 × 1 metric × ~90s/题 ≈ **20 min**
- 或扩到 5 题 × 5 变体 × 1 metric ≈ **35 min**

**先只跑 ctx_recall 一个 metric**（最直接验假设的指标，省 3/4 token）。

## 5. 待跑命令清单（按顺序）

> 全部命令请在 host shell 跑（**不在 docker exec 里**），`cwd=/data/3GPP-Everything`。
> 这些命令都是 read-only 或仅产新 `eval-results/` 目录，**不动 prod / golden**，可
> 放心跑（不触发 CLAUDE.md §5 任何禁区）。

### 5.1 [先跑] ground_truth 变体消融（确认哪个变体最优）

```bash
cd /data/3GPP-Everything && uv run --project eval python -m \
    eval.scripts.ablate_ground_truth \
    --results eval-results/v6-citation-index-20260529T092708Z-ragas/results.json \
    --golden eval/golden/v1.yaml \
    --bm25-dir /data/tgpp/bm25/voyage \
    --item-ids hand-multi-007,hand-table-008,hand-formula-006 \
    --metrics context_recall 2>&1 | tee /tmp/ablate_ctxrecall.log
```

期望输出（在末尾的 `==== SUMMARY (context_recall) ====` 节）：

```
item_id          BASELINE  ORIG  SENT  WRAPPED  SECTION  HYBRID
hand-multi-007     0.00    0.00  0.??  0.??     0.??     ≥0.5 ✓
hand-table-008     0.00    0.00  0.??  0.??     0.??     ≥0.5 ✓
hand-formula-006   0.00    0.00  0.??  0.??     0.??     ≥0.5 ✓
```

判定规则：HYBRID 列至少 2/3 题 ≥ 0.5 → 进入 §5.4 全量重跑；
否则尝试 §5.5 备选 reference 方案（加章节内容的更长截断）。

预估时间：~20–30 min。

### 5.2 [并行可跑] 救 9 题 ragas None

> **不要和 §5.1 同时跑**（LLM proxy 共享 ~3.5 calls/min；v6 handoff §8 已踩坑）。
> 跑完 §5.1 再跑这个。

```bash
# 先把 v6 ragas 结果拷一份，方便就地更新
cp -r eval-results/v6-citation-index-20260529T092708Z-ragas \
      eval-results/v6-ragas-retry-2026-05-29

cd /data/3GPP-Everything && uv run --project eval python -m \
    eval.scripts.retry_failed_ragas \
    --results eval-results/v6-ragas-retry-2026-05-29/results.json \
    --golden eval/golden/v1.yaml \
    --bm25-dir /data/tgpp/bm25/voyage \
    --timeout 600 --workers 4 -v
```

期望：9 题里 OK ≥ 7；STILL-TIMEOUT ≤ 2；EMPTY-CTX = 0。
脚本会就地更新 `results.json` 和重写 `report.md`。

预估时间：~15–25 min（9 题 × ~120s/题）。

### 5.3 [§5.1 跑完后] 把 HYBRID 改造写进 `eval/ragas_eval.py`

只有 §5.1 实验确认 HYBRID 是赢家才做。修改点：

1. `eval/ragas_eval.py`：`_ground_truth(item)` 加形参 `chunk_index: dict[str, list[dict]] | None = None`；
   带 chunk_index 时走 HYBRID 构造；不带时回退当前行为（保留向后兼容）。
2. `eval/scripts/rejudge_results.py`：在 main 里加载 `chunks_by_spec` 一次，
   把 lookup 透传给 scorer.score_item（需要把 chunk_index 传到 _ground_truth；
   可考虑给 `RagasScorer` 加 `chunk_index` 字段或者 ad-hoc 参数）。
3. 加 unit test：`eval/tests/test_ragas_eval.py`（新文件或扩现有）
   - test_ground_truth_hybrid_returns_section_content
   - test_ground_truth_fallback_when_no_chunk_index
   - test_ground_truth_truncate_long_sections
4. 跑 `make lint` 和 `cd eval && uv run pytest` 确保不破坏现状。

### 5.4 [§5.3 完成后] 用 HYBRID + retry 跑完整 56 题 ragas

```bash
mkdir -p eval-results/v7-hybrid-gt-$(date +%Y%m%dT%H%M%SZ)
OUT=eval-results/v7-hybrid-gt-$(date +%Y%m%dT%H%M%SZ)

cd /data/3GPP-Everything && uv run --project eval python -m \
    eval.scripts.rejudge_results \
    --results eval-results/v6-citation-index-20260529T092708Z/results.json \
    --golden eval/golden/v1.yaml \
    --bm25-dir /data/tgpp/bm25/voyage \
    --out-dir $OUT \
    --run-label v7-hybrid-gt \
    --skip-negative \
    --skip-fact-judge \
    -v 2>&1 | tee $OUT/run.log
```

跑完看 `$OUT/report.md` 的 4 项指标。预估时间：56 题 × ~90s/题 ≈ 90 min。

> ⚠️ deepseek-v4-pro 推理模式 + LLM proxy 限流；这一步只跑这一个进程，
> 不要并发 retry_failed_ragas / daily eval。

### 5.5 [§5.4 仍未达标的备选] 如果 HYBRID 验完仍 < 0.75

按以下顺序排查（一次只动一个变量）：

1. **重跑取均值**：跑完 §5.4 立刻再跑一次（不同种子），看均值是否 ≥ 0.75；
   单次跑方差大（§4.1 实测过同题 0.00 → 0.333 跨 run 差异）。
2. **加大 SECTION 截断**：3000 → 5000 chars；牺牲 timeout 换 coverage。
3. **改 ragas judge 模型**：deepseek-v4-pro 推理慢且抠字；试 `mimo-v2.5-pro`
   (function_calling 支持的非推理模型) 看是否打分更宽松且更稳定。
   ⚠️ 要保持 cross-vendor anti self-bias（不和 generation 同源），不要换成生成同
   一家的；mimo-v2.5-pro 已经是 agent 主脑，**不能**用它做 judge。可考虑回退到
   `glm-5.1`（M7.6 之前用的），但成本会上去。**这条改动需要人审**（CLAUDE.md §5.7
   依赖大改动）。
4. **审 answer_relevance=0 四题**（§3.3）；若 §5.4 后 ans_rel < 0.75，跑：

   ```bash
   uv run python - <<'PY'
   import json
   d = json.load(open('eval-results/v6-citation-index-20260529T092708Z-ragas/results.json'))
   for r in d['results']:
       if r.get('ragas_answer_relevance') == 0:
           print(r['item_id'])
           print(r['answer'][:600])
           print('---')
   PY
   ```

   逐题判 真 noncommittal vs 误判，按 §3.3 处理。

## 6. 附录：诊断脚本完整输出（已实跑）

### 6.1 hand-multi-002（diagnose_ctx_recall.py 实跑）

```
ground_truth (full): '672 N_ID,1 N_ID,2 m = (n + 22 + 43 * N_ID,2) mod 127'
contexts count: 2
[0] [38.211 § 8.4.2.1] There are 672 unique physical-layer sidelink synchronization
    identities given by where id_net and id_oon. ...
[1] [36.213 § 14.4] PSSS power formulas ...
==> ragas context_recall = 0.333  (v6 baseline 此题 = 0.00；跨 run 方差)
```

**判定**：召回的 chunk 缺 8.4.2.2.1 那段（m=... 公式），即使 reference 写法
完美也救不回 m 这条 fact；section_recall_substring 因 8.4.2.1 命中而误判 1.0。

### 6.2 hand-formula-001（diagnose）

```
ground_truth: 'a_k,l e^(j2\pi(k+k_0)) \Delta f N_grid N_sc^RB'
contexts count: 2
[0] [38.211 § 5.3.1] The time-continuous signal on antenna port and subcarrier spacing
    configuration for OFDM symbol l ... is defined by\n\nwhere at the start of the subframe,
    \n\nand\n\n- is given by clause 4.2; ...
[1] [38.211 § 5.3.1] UCCH transmissions ...
==> ragas context_recall = 0.0
```

**判定**：chunk 抽取时 latex block 整段没保留（"defined by\n\nwhere" 后面紧接空白），
真正的 OFDM 公式在 chunk 里就是不存在。**ingestion 层问题**，单列 §3.4。

### 6.3 hand-table-008（diagnose）

```
ground_truth: "表格 5.4-1 符号数 Z1 25 Z'1 21"
contexts count: 1
[0] [38.214 § 5.4 UE CSI computation time]
    | $\mu$ | $Z_1$ [symbols] |
    | 0 | 10 | 8 |
    | 1 | 13 | 11 |
    | 2 | 25 | 21 |
    | 3 | 43 | 36 |
    Table 5.4-2: CSI computation delay requirement 2
==> ragas context_recall = 0.0
```

**判定**：context 完美包含 `| 2 | 25 | 21 |` 行；ctx_recall=0 **纯粹因为
中文 reference 拆出来的 statement (符号数 / 表格 5.4-1 ...) 在英文表格里
找不到 token 对应**。这种题型用 SECTION/HYBRID reference 应该能直接救回。

### 6.4 hand-multi-007（diagnose）

```
ground_truth: '冗余版本 sequenceOffsetforRV repK-RV 附加移位操作 第一传输时机'
contexts count: 1
[0] [38.214 § 6.1.2.1] For both PUSCH repetition Type A and Type B, when DCI format 0_1 or 0_2
    indicates codepoint "10" or "11" for the SRS resource set indicator, the redundancy version
    to be applied on the nth transmission occasion ... Table 6.1.2.1-2 ... sequenceOffsetforRV ...
==> ragas context_recall = 0.0
```

**判定**：context 完美匹配英文术语 `redundancy version`、`sequenceOffsetforRV`、
`first transmission occasion`；reference 全中文 token 在英文 chunk 里 0 attribution；
WRAPPED 或 HYBRID 应该可以救回（英文句模板 + 英文章节内容拼一起）。

## 7. 风险 / 看护点

| 风险 | 评估 | 缓解 |
|---|---|---|
| ragas 单题方差大（§4.1 实测同题 0 → 0.333） | 中 | §5.4 跑完后再跑一次取均值 |
| HYBRID reference 字数大触发 evaluate timeout | 中 | 章节内容 truncate 到 3000 chars 起步；retry 用 timeout=600 |
| §3.4 ingestion 层 2-3 题救不回 | 低 | 接受，全 38/40 题达标即可推过 0.75 |
| 改完 ragas_eval 影响 daily/weekly CI 默认行为 | 中 | 新增 chunk_index 参数走 opt-in；不传参数时回退当前行为 |
| 跨 run 不可重复 → 阈值守护无意义 | 低-中 | 阈值定 0.75 时给 0.03 容差；single-run 跑 2 次取低值入 baseline |
| 评测口径变更（reference 从 facts 变成 facts+section） | 中 | 立新 baseline（类似 v6 那次），handoff 里讲清楚，CLAUDE.md §5.6 这种是"评测方法升级"不是降级 |

## 8. 完成条件 / 退出准则

本任务退出的硬门禁（必须全绿）：

- [ ] `eval/ragas_eval.py` 改完 + `cd eval && uv run pytest` 全绿
- [ ] `make lint` 全绿
- [ ] §5.4 跑完一次 + 复跑一次，**两次均值**:
  - faithfulness ≥ 0.75
  - context_precision ≥ 0.75
  - context_recall ≥ 0.75
  - answer_relevance ≥ 0.75
- [ ] 新 baseline 帖（类似 `2026-05-29-v6-citation-index-eval-findings.md` 那种）落到
      `docs/04-handoff/2026-05-29-ragas-4metric-uplift-results.md`，更新 §1.4 阈值守护
      和这份计划帖的对照
- [ ] 把 `eval/scripts/diagnose_ctx_recall.py` 和 `ablate_ground_truth.py` 决定保留
      为常驻 debug 工具 / 删掉；若保留写一句 docstring 说"verifier"（不算 dev infra）

若两次均值仍未达标 → 走 §5.5 排查；连续 3 次仍未达标 → 停下来按 CLAUDE.md §5.9
向人汇报，**不**继续盲改。

## 9. 跑这一坨需要消耗多少 token / 时间

| 步骤 | LLM 调用估算 | 时间 |
|---|---|---|
| §5.1 ablate（3 题 × 5 变体 × 1 metric） | ~30 deepseek 调用 | 20–30 min |
| §5.2 retry（9 题 × 4 metric × ~5 statements） | ~60 deepseek 调用 | 15–25 min |
| §5.3 改代码 + unit test | 0 | 30 min |
| §5.4 full rejudge（56 题 × 4 metric） | ~700 deepseek 调用 | 90 min |
| §5.5 复跑 §5.4 | ~700 deepseek 调用 | 90 min |
| 合计 | ~1.5k deepseek 调用 | **~4–4.5 h**（顺序跑） |

deepseek-v4-pro 单价 ¥3/¥6 per M（input/output），按平均一次调用 1k input / 500 output
估算，总成本 ≈ 1500 × (1×3 + 0.5×6) / 1e6 = ¥9，约 1.2 USD，可控。

## 10. 改起来的最小 diff 草图（让重启 agent 一眼能上手）

### `eval/ragas_eval.py` 改动点（草稿）

```python
@dataclass(slots=True)
class RagasScorer:
    llm: Any
    embeddings: Any
    metrics: list[_RagasMetric]
    chunk_index_by_spec: dict[str, list[dict]] | None = None  # ← 新增

    def score_item(self, item, resp, *, run_config=None):
        # ... 原有逻辑 ...
        row = {
            ...
            "ground_truth": _ground_truth(item, self.chunk_index_by_spec),
            "reference":   _ground_truth(item, self.chunk_index_by_spec),
            ...
        }


def _ground_truth(item, chunk_index_by_spec=None):
    facts = [str(f).strip() for f in (item.expected_facts or []) if str(f).strip()]
    facts_part = ". ".join(f"The answer should mention {f}" for f in facts)
    if facts_part:
        facts_part += "."

    section_text = ""
    if chunk_index_by_spec and item.expected_specs:
        chunks = []
        for es in item.expected_specs:
            spec_chunks = chunk_index_by_spec.get(es.spec_id, [])
            sec_prefixes = [tuple(s.split(".")) for s in (es.sections or [])]
            for c in spec_chunks:
                clause = str(c.get("clause") or "")
                cparts = tuple(clause.split(".")) if clause else ()
                # 同前缀匹配（"4.4.4" 命中 "4.4.4", "4.4.4.2" 等）
                if any(cparts[:len(p)] == p for p in sec_prefixes if p):
                    chunks.append(c.get("content") or "")
        if chunks:
            section_text = "\n\n---\n\n".join(dict.fromkeys(chunks))[:3000]

    if facts_part and section_text:
        return f"{facts_part}\n\nReference section content:\n{section_text}"
    if facts_part:
        return facts_part
    if section_text:
        return section_text
    if item.expected_specs:
        return " ".join(s.spec_id for s in item.expected_specs)
    return "(no ground truth)"
```

### `eval/scripts/rejudge_results.py` 改动点（草稿）

在 main 里 build 完 `chunk_idx`（chunk_id → content 平表）后，
再 build 一个 `chunks_by_spec`（spec_id → list[chunk_record]），传入 scorer：

```python
chunks_by_spec = build_chunks_by_spec(args.bm25_dir, needed_specs)  # 新增 helper
scorer = build_default_ragas_scorer(settings, chunk_index_by_spec=chunks_by_spec)
```

`build_default_ragas_scorer` 加个可选参数往 RagasScorer 透传 chunk_index_by_spec。

### unit test 草稿（`eval/tests/test_ragas_eval_ground_truth.py` 新文件）

```python
from eval.ragas_eval import _ground_truth
from eval.runner_retrieval import GoldenItem, ExpectedSpec


def test_no_chunk_index_keeps_facts_as_wrapped_sentences():
    item = GoldenItem(id="x", category="def", question="?", expected_specs=[],
                     expected_facts=["672", "N_ID,1"], forbidden=[], must_say_not_found=False,
                     source="hand", language="zh")
    gt = _ground_truth(item)
    assert "The answer should mention 672" in gt
    assert "The answer should mention N_ID,1" in gt


def test_with_chunk_index_appends_section_content():
    item = GoldenItem(
        id="x", category="def", question="?",
        expected_specs=[ExpectedSpec(spec_id="38.211", sections=["8.4.2"])],
        expected_facts=["672"], forbidden=[], must_say_not_found=False,
        source="hand", language="zh",
    )
    chunks_by_spec = {
        "38.211": [
            {"clause": "8.4.2.1", "content": "There are 672 unique sidelink ids"},
            {"clause": "9.1", "content": "Unrelated section"},
        ]
    }
    gt = _ground_truth(item, chunks_by_spec)
    assert "There are 672 unique sidelink ids" in gt
    assert "Unrelated section" not in gt


def test_long_section_is_truncated():
    long_content = "X" * 5000
    item = GoldenItem(
        id="x", category="def", question="?",
        expected_specs=[ExpectedSpec(spec_id="38.211", sections=["1"])],
        expected_facts=[], forbidden=[], must_say_not_found=False,
        source="hand", language="zh",
    )
    chunks_by_spec = {"38.211": [{"clause": "1.1", "content": long_content}]}
    gt = _ground_truth(item, chunks_by_spec)
    assert len(gt) <= 3500  # 3000 + 模板字符
```

---

> 下次开工先读 §0 TL;DR + §5 命令清单。其他章节当 reference 用。
