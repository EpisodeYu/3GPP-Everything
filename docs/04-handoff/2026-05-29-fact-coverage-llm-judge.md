# fact_coverage 评分由 substring 改为 LLM judge

> 2026-05-29 · 增量改进（M7 完成后）
>
> 上下文：[`2026-05-29-v6-citation-index-eval-findings.md §5`](2026-05-29-v6-citation-index-eval-findings.md)
> 决策依据：[`2026-05-23-m7.7-teleqna-golden-repair.md` §收尾]
> 守则：CLAUDE.md §4.1（自动化测试是"完成"硬门槛）/ §3（surgical changes）

## 1. 背景

v6 citation index daily eval（56 题手写子集）相对 v5 baseline：

| 指标 | v5 baseline | v6 | Δ | 备注 |
|---|---|---|---|---|
| `fact_coverage` (substring) | 35.21% | 30.32% | **−4.88pp** | 大部分 metric artifact |
| `ragas_faithfulness` | 0.524 | **0.650** | +12.63pp | 真信号：v6 更 grounded |

四道掉分最厉害的题（`hand-table-007 −0.43` / `hand-multi-001 −0.38` /
`hand-proc-003 −0.33` / `hand-proc-004 −0.25`）抽样原因：

- **table-007**：baseline 自信引用具体数字（实际是 chunk 数据冲突时挑了一个），
  fc=0.86；v6 明确说"未包含 Table 6.1.4.1-2 的完整数据内容""无法给出确切的目标
  码率和频谱效率数值"，因此没命中 expected_facts 里的具体数字。**v6 更诚实但
  fact_coverage 扣分**。
- **multi-001**：baseline 列了 `QPSK / 16QAM / 64QAM / 256QAM / 1024QAM`，命中 5
  个事实；v6 说"未列出 PDSCH 具体支持的调制方案的枚举"，不命中。**也是诚实的代价**。
- **proc-003 / proc-004**：v6 答案语义完全正确（描述了截断 MSB、I_MCS 最高的传输
  块）但换了一个措辞——`expected_facts` 是"使得 DCI format 0_0 的大小等于 DCI
  format 1_0 的大小"，v6 写"使其大小与 DCI format 1_0 相等"，**子串匹配失败**。
  纯 paraphrasing variance。

> `expected_facts` 用 case-insensitive 子串匹配，对答案措辞敏感。两次 LLM 运行的
> paraphrasing variance 在 daily harness 上通常吃掉 2-3pp。真正的语义忠实度要靠
> ragas faithfulness，daily 不算，要 weekly 路径才出。

→ 决议：彻底改 LLM judge。

## 2. 改动总览（surgical）

新增 1 文件 + 改 4 文件 + 加 30 单测；不动 retrieval / agent / golden 任何东西。

```
eval/fact_coverage_judge.py        # 新建：LLM judge 实现 + 工厂
eval/settings.py                   # +1 行：llm_fact_coverage_judge_model 配置
eval/runner.py                     # 字段 + run_eval hook + aggregate 双轨 + Langfuse 三路
eval/scripts/rejudge_results.py    # 加 --skip-fact-judge + 重判 hook
backend/tests/eval/test_golden_v1.py   # daily / weekly 默认尝试 build judge
eval/tests/unit/test_fact_coverage_judge.py    # 新建：19 单测
eval/tests/unit/test_runner.py     # +11 单测：注入 / fallback / aggregate
docs/03-development/06-evaluation-and-observability.md   # §4 表 + §12 增量改进段
```

## 3. 设计要点

### 3.1 一题一次 LLM call，逐条 fact 独立判

`FactCoverageJudge.score_item(item, resp)` 把 `expected_facts` 列表整个塞进一个
prompt，让 LLM 一次性返回 `[{fact, verdict, reason}, ...]` 与 facts 一一对应。
对比"一条 fact 一次 call"省 ~3x token + 上下文一致性更好。

返回结构：

```python
{
    "score": 0.83,            # weighted = (HIT*1 + PARTIAL*0.5) / total
    "verdicts": [             # 与 expected_facts 顺序一一对应
        {"fact": "QPSK", "verdict": "HIT", "reason": "..."},
        {"fact": "1024QAM", "verdict": "MISS", "reason": "..."},
    ],
    "skipped": False,         # 空答案 / 空 expected_facts → True, score=None
    "reason": None,           # judge_error / skipped 摘要
}
```

### 3.2 三档 verdict + 严格 prompt 规则

- **HIT**：答案以事实层面陈述了该 fact。允许同义改写 / 重新组织 / 数值等价 /
  单位换算 / 顺序差异。**不要求**字面一致。
- **PARTIAL**：提到话题但缺漏一半 / 仅一侧 / 措辞偏差到边界。
- **MISS**：完全没覆盖，或明确说"未在资料中找到"。

prompt 里写死的硬约束：

- 数值类 fact 必须出现该数值才能 HIT（等价形式 / 单位换算允许；"未给出具体数值"
  → MISS）。
- 拒答 / "未找到"答案应当大量给 MISS（**不因诚实就放水**给 PARTIAL/HIT；这点是
  v6 表达诚实但 fact 未覆盖的真实信号）。
- 不因为答案提到"相邻 / 相关概念"就 HIT。

### 3.3 单题异常隔离 + fallback substring

```
fact_coverage（主字段，daily 阈值断言 + Langfuse evaluator filter 都挂这个）
   ├─ judge 成功 → judge score
   ├─ judge 失败（LLM 崩 / schema 不合法 / 全条 verdict 不合法）→ substring
   ├─ judge 未注入（缺 LITELLM_API_KEY / 包未装）→ substring
   └─ expected_facts 空 → None（与原行为一致）

fact_coverage_judge      # 纯 LLM judge 值；judge 缺 / 异常 → None
fact_coverage_substring  # 纯 substring 值；compute_eval_metrics 永远会算
fact_coverage_judge_details  # per-fact verdict 明细，落 results.json 用于诊断
```

故 daily smoke / 无 LLM key 环境（CI ASGI in-process 测试）依旧能拿到
`fact_coverage`。Langfuse 三路同上：`fact_coverage` 主轴 +
`fact_coverage_judge` / `fact_coverage_substring` 双轨诊断。

### 3.4 judge model = `mimo-v2.5-pro` + bind_tools（自解析 tool_call）

沿用 negative_judge 的 LiteLLM 通路。**不**用 deepseek-v4-pro 是因为它的 reasoning
mode 不支持 `tool_choice` → `with_structured_output(method="function_calling")`
会 400（详见 `eval/negative_judge.py` 顶部注释 + `06-md §5`）。

可在 `eval/settings.py::llm_fact_coverage_judge_model` 单独配，与 ragas judge /
negative judge 解耦。

> **同源偏差风险**：mimo-v2.5-pro 也是 agent 模型。prompt 里的硬约束（数值类必
> 须出现、拒答应该大量 MISS）是首道防线。weekly 跑后看 judge vs ragas
> faithfulness 的相关性，若发现 judge 假阳性多于 ragas，再考虑切到 deepseek-v4
> JSON mode（不走 function_calling）。

### 3.5 实施过程踩到的坑：mimo verdicts JSON-string 编码

**现象**（2026-05-29 14:05 v6 56 题首跑）：mimo-v2.5-pro 在 function_calling
返回时偶发把 `verdicts` 字段编码成 JSON-encoded 字符串而非数组。
56 题里 hand-def-001 / hand-def-003 命中（~3% 复现率）：

```text
fact_coverage_judge crashed on hand-def-003: 1 validation error for _Schema
verdicts
  Input should be a valid list [type=list_type,
    input_value='[{"fact": "传输块的...资料中找到。"}]', input_type=str]
```

**第一次修法（无效）**：在 `_Schema.verdicts` 加 pydantic
`field_validator(mode="before")` 自动 `json.loads`。本地 `S(**{"verdicts": "[...]"})`
能 work，但 langchain 1.4 / pydantic 2.13 的 `PydanticToolsParser` 实际生产
path 上 before-validator 未被触发（未深查 langchain 内部，多半是用了别的
实例化路径）。

**第二次修法（采用）**：改走 `llm.bind_tools([_Schema], tool_choice=..., parallel_tool_calls=False)`，自己解析
`ai_msg.tool_calls[0]["args"]`，让 `_normalize_verdicts_field()` 在解析阶段
先 `json.loads` 一次。两道防线（schema 上的 `_pre_parse_verdicts` 仍保留作
defense-in-depth）。

后续完整跑（2026-05-29 14:14-14:27）：56 题 0 crash，13.5 分钟跑完。

## 4. 配置选择记录

| # | 选项 | 拍板 | 理由 |
|---|---|---|---|
| A | judge model | **mimo-v2.5-pro**（function_calling）| 沿用 negative_judge 通路，0 新坑 |
| B | 档次 | **三档 HIT/PARTIAL/MISS，PARTIAL=0.5** | 与 negative_judge 风格一致；多一档便于诊断"语义对了但缺一半"那种 |
| C | 双轨过渡 | **直接换**（不做 7 天双轨观察期） | substring 字段保留作诊断 + LLM 失败 fallback；主指标直接切 judge |
| D | 主字段语义 | **跟 judge，缺 judge fallback substring** | daily 阈值断言 + Langfuse evaluator filter 已经写在 `fact_coverage` 名字上，不改名最省事 |
| E | 缺 LLM key 时 | **退化为 substring**（旧行为） | CI 偶尔跑无 key 模式不至于把 daily 测试搞挂 |
| F | daily 新阈值 | **等修改后跑一次再定** | 量级会跳，盲拍数没意义 |

## 5. 自测结果

```
$ uv run --project eval pytest eval/tests/unit/ -q
459 passed in 10.50s

$ cd backend && uv run pytest tests/eval/test_golden_v1.py::test_runner_smoke_against_canned_backend -q
1 passed in 4.41s
```

新增覆盖：

- `eval/tests/unit/test_fact_coverage_judge.py`（33 单测）：
  - HIT/PARTIAL/MISS 三档 + lowercase 归一化
  - 空答案 / 空 expected_facts / 全空白 facts → skipped
  - LLM 漏判 / 多判 / 未知档 → 该条 verdict=None；全条不合法 → score=None
  - 非 list shape → 拒判
  - LLM crash → judge_error 兜底
  - 中英 prompt 都 runnable
  - 缺 `LITELLM_API_KEY` → `FactCoverageJudgeError`
  - **`_pre_parse_verdicts` validator**（schema 兜底；5 单测）：JSON 字符串
    list 自动解析 / 非合法 JSON / dict shape / 原 list 透传 / 实际 mimo 样本
  - **`_normalize_verdicts_field`**（生产 path 兜底；6 单测）：list / JSON 字符串 / dict shape / garbage / None / 真实 mimo 样本
  - **`bind_tools` 生产 path**（4 单测）：string verdicts 归一化、原生 list、
    无 tool_call 兜底为 judge_error、args 非 dict 兜底
- `eval/tests/unit/test_runner.py`（+11 单测）：
  - judge 成功 → 主字段切到 judge 值，substring 字段保留
  - judge 抛异常 → 主字段 fallback substring
  - empty expected_facts / empty answer → judge 早 return（不打 LLM）
  - aggregate 三路均值 + judge=None 不进 judge mean

ReadLints：无 error/warning。

## 5.1 v6 daily 56 题实测（2026-05-29 14:14-14:27）

`eval/scripts/rejudge_results.py` 跑 v6 baseline（`eval-results/v6-citation-index-20260529T092708Z`）的 56 题 →
`eval-results/2026-05-29-fact-judge-poc/{results.json,report.md}`。
**0 crash，13.5 分钟**（mimo-v2.5-pro，平均 ~25s/题，串行）。

### 整体对比

| 指标 | substring | LLM judge | Δ |
|---|---:|---:|---:|
| **fact_coverage** | 0.303 | **0.647** | **+34.4pp** |
| forbidden_hit_rate | 0.464 | 0.464 | 0 |
| context_recall_section | 0.825 | 0.825 | 0 |
| context_recall_spec | 0.925 | 0.925 | 0 |
| negative weighted_pass | 1.000 | 1.000 | 0 |

### 4 道用户提到的"掉分最厉害的题"

| Item | substring | judge | judge 判定 | 验证 |
|---|---:|---:|---|---|
| **hand-proc-003** (procedure) | 0.000 | **1.000** | 全 3 条 HIT —— "通过截断 MSB" / "减小位宽" / "使大小相等"全语义命中 | ✅ 救活 paraphrase |
| **hand-proc-004** (procedure) | 0.000 | **1.000** | 全 4 条 HIT —— "I_MCS 最高" / "highest I_MCS" / "I_MCS 相同" / "复用第一传输块" | ✅ 救活 paraphrase |
| **hand-multi-001** (multi_section) | 0.250 | **0.000** | 全 8 条 MISS —— agent 说 "无法根据 chunks 回答支持哪些方案" | ✅ 严格判：诚实拒答 ≠ 事实覆盖 |
| **hand-table-007** (table_lookup) | 0.429 | **0.000** | 全 7 条 MISS —— agent 说 "无法给出确切数值" + 引错表 | ✅ 严格判：未给值就是 MISS |

判定模式与设计预期完全一致：

- paraphrase / 数值等价 → HIT（救起 substring 假阴性）
- 诚实拒答 / "未找到" → MISS（修正 substring 假阳性，agent 答了相关词就部分命中）
- 引错表 / 数值不出 → MISS（hand-table-007：substring 把"目标码率"/"频谱效率"当部分命中，judge 拒绝）

### 按 category（n=8/类，negative 16 题 expected_facts 空 → 不计）

| category | judge fact_cov | 解读 |
|---|---:|---|
| definition | **0.881** | 最高，定义题 ground truth 直接命中 |
| procedure | 0.781 | proc-003/004 救活后整体提升 |
| formula | 0.561 | 公式类一半含具体表达式，覆盖参差 |
| table_lookup | 0.552 | agent 拒答数值类 → 真 MISS，分确实低 |
| multi_section | 0.460 | 最低，multi-section 答案最不全（M8 已知短板） |

### 决策（F 选项）：daily 阈值建议

substring 时代实际跑值：v5 baseline=0.352 / v6=0.303。
LLM judge 实际跑值：v6 hand_crafted=**0.647**。

建议 **新阈值 `fact_coverage ≥ 0.55`**（保留 ~10pp 容差，覆盖 LLM judge run-to-run
variance + 特别是 multi_section / table_lookup 这两类天然偏低的类别）。
也可以分类别设：definition/procedure ≥ 0.75，其余 ≥ 0.45。先单一阈值，
观察 7 天 daily 后再分级。

> **未来更严的阈值**（M8+）：随着 multi_section / table_lookup 答题质量提升
> （参 docs/04-handoff/2026-05-29-v6-citation-index-eval-findings.md §4），
> 整体 fact_coverage 应能爬到 0.7+，届时阈值再上调到 0.65。

## 6. 自主决策记录

按 `CLAUDE.md §4.3` 自主决策的：

- 双轨字段命名：`fact_coverage_substring` / `fact_coverage_judge` /
  `fact_coverage_judge_details`（与现有 `negative_judge_*` 命名风格对齐）
- LLM 漏判一条时的兜底：缺位的 fact 视作 verdict=None，分数按 `len(facts)` 做
  分母（不让 LLM 漏返 = 抬分；与 negative_judge "unjudged 不算" 风格一致）
- prompt 中英分版按 `item.language` 选，与 negative_judge 一致
- `_invoke_structured` 不强转 `verdicts` list（`list("not a list")` 会拆字符
  掩盖错误 → 透传给 caller 用 `isinstance(..., list)` 检测）

## 7. 留给人审 / 后续

- [x] **跑一次 v6 子集双轨对比**（2026-05-29 14:14-14:27 完成）：4 道掉分题与
      56 题整体对比见 §5.1；judge 行为完全符合"paraphrase HIT、诚实拒答
      MISS、数值不出 MISS"的设计预期
- [x] **据此定 daily 新阈值**（F 选项 2026-05-29 落）：建议 `fact_coverage ≥ 0.55`
      （详见 §5.1 末段；分类别细化阈值留给观察 7 天 daily 后再上）
- [x] **06-md §7 阈值表更新 + daily harness assert**（2026-05-29 同日落）：
      宽松档 `fact_coverage ≥ 0.55` / 严格档 ≥ 0.65（M8 上线前 PR 再收紧）；
      `backend/tests/eval/test_golden_v1.py::test_golden_v1_daily` 加 assert
- [ ] **mimo 自评偏差监控**：weekly 跑后看 `fact_coverage_judge` vs
      `ragas_faithfulness` 的相关性；若发现 judge 假阳性 ≫ ragas，切 deepseek-v4
      JSON mode 是预案 B（成本：单 judge call 多算 ~30-60s reasoning latency）

## 8. 风险与排雷

| 风险 | 应对 |
|---|---|
| LLM judge 自身有 run-to-run variance | temperature=0.01 压住绝大多数；周报看 7 天移动平均 |
| 数值类 fact 在 PARTIAL 边界 LLM 判不稳 | prompt 硬约束"必须出现该数值才 HIT" + 单测验证；2026-05-29 后跑 v6 子集 sanity check |
| function_calling 通路在 mimo 升级后炸 | 与 negative_judge 共用通路，回归测试覆盖；切 deepseek-v4 JSON mode 是预案 |
| 历史 results.json 没有新字段 | `eval/scripts/rejudge_results.py --skip-fact-judge=false`（默认开）一键重判 |
| daily 主指标 `fact_coverage` 量级跳变 | 主字段绝对值会上一档，趋势线短期断裂；新阈值由首次 v6 双轨结果定（§7） |

## 9. 参考

- `eval/fact_coverage_judge.py` — 实现
- `eval/negative_judge.py` — 同款通路的参照
- `eval/runner.py::EvalResult` / `compute_eval_metrics` / `run_eval` — 字段与 hook
- `06-evaluation-and-observability.md §4.1` — 指标实现口径表
- `2026-05-23-m7.7-teleqna-golden-repair.md` — 第一次记录"考虑 LLM semantic judge"
- `2026-05-29-v6-citation-index-eval-findings.md` — 触发本次决议的 v6 daily
