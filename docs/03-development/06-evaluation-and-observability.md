# 03·06 - 评测与可观测性

> "生产级"的硬指标在这里落地。覆盖：金标准评测集、自动评测（Ragas）、Langfuse 监控、成本告警。

## 0. M7 执行顺序

> 2026-05-19 拆解。M7 拆 7 段，按下表顺序推进，每段门禁全绿才进下一段；同段子项可并行。
>
> **2026-05-19 人 approve 决策**（详见 [`../04-handoff/2026-05-19-m7-plan.md §3`](../04-handoff/2026-05-19-m7-plan.md)）：
>
> - **Q1 跑测节奏**：全集（v1.yaml 总 ≥ 140 题）每周一次 + 手写题（`source==hand_crafted`，≥ 20 题）每日跑。预算估月度 ¥35（远低于 ¥1000 警戒线）
> - **Q2 阈值告警通道**：仅 log warning（不接 webhook，零 secret 维护）；M8 上线视监控需要再加
> - **Q3 D13 两档阈值**：M7 nightly 用宽松版（faithfulness ≥ 0.75 / context recall ≥ 0.65 / answer relevancy ≥ 0.70 / answer correctness ≥ 0.55 / latency-p50 ≤ 6s / cost-p50 ≤ ¥0.30）；M8 上线门槛用严格版（≥ 0.85 / 0.80）。沿用 [`2026-05-18-tech-debt-cleanup-todo.md` Q1](../04-handoff/2026-05-18-tech-debt-cleanup-todo.md) 决议
> - **Q4 spec 查看方式**：选 Swagger UI（M4.10 已就位 `/docs`），M7 不再补 `eval spec` CLI

| 子里程碑 | 主要交付物 | 完成度门禁 |
|---|---|---|
| **M7.0** 金标准 v1 → v1.5 ✅ 2026-05-20 | `eval/golden/_template.yaml` 模板 + `eval.cli golden validate/merge/stats` 子命令 + 手写补题（neg / formula / multi_section 重点；2026-05-19 砍 `tool` category） | v1.yaml 题数 ≥ 140；分布按 §3.4 容差 ±5 题；`[human]` 至少 20 题人审过（题数 175 / 手写 56 ≥ 20 已达；human review 待办） |
| **M7.1** 端到端 runner + 第一档阈值 ✅ 2026-05-20 | `eval/runner.py`（HTTP `/chat` SSE → metrics → report.md/json）；`backend/tests/eval/test_golden_v1.py` 落 D13 第一档断言；Makefile `eval-daily/eval-weekly` | unit + integration 全绿；smoke（canned graph）all green；daily/full live 断言需 `RUN_LIVE_EVAL=1`（M7.6 CI 触发） |
| **M7.2** Ragas + native MCQ ✅ 2026-05-20 | Ragas 4 metric 接入（judge=`glm-5.1`，避免同源偏差）；`eval/scripts/native_mcq_runner.py`（TeleQnA 选择题对照） | `eval/ragas_eval.py` + runner hook + 56 单测全绿（27 ragas + 29 mcq）；MCQ runner 一键 `eval native-mcq run` → `eval-results/m7-native-mcq/{ts}/report.md` |
| **M7.3** Langfuse Dataset 集成 ✅ 2026-05-20 | `eval/langfuse_dataset.py` 一次性 push 金标准；runner 每条 item 上传 score（fact_coverage / faithfulness 等） | code 完成 + 21 单测全绿；`[human]` 待启用 built-in evaluators（M7.2 评估结束后人触发首次推送） |
| **M7.4** 成本与用量监控 | `backend/app/services/usage.py` + `app/llm/pricing.py` + `services/alerts.py`（仅 log）；LiteLLM 响应钩 `usage` 字段 → ApiUsage upsert | unit 覆盖 LLM/Embed/Rerank/WebSearch 4 路径；`/admin/stats` 真实数据；alerts 阈值触发 → log warning（mock 验证） |
| **M7.5** Batch C 技术债（retrieval 校准） | C.2 R10/R11/R19 retrieval 校准（数据 drive 调 dense/RRF/rerank top_k）；C.3 O2 rerank ablation 报告 → `eval-results/m7-rerank-ablation.md`；C.4 `test_retrieve_node_p50_latency_under_800ms` 处理 | C.2：daily eval 连跑 2 次 ≥ 第一档阈值；C.3：报告归档；C.4：阈值放宽或 outlier 处理 |
| **M7.6** Daily/Weekly CI + 完成验收 | `.github/workflows/eval-daily.yml` cron 02:00 跑 daily / `eval-weekly.yml` cron 周一 03:00 跑全集；阈值未达自动开 issue（mock 验证） | nightly 连跑 2 次 ≥ D13 第一档；交付 `docs/04-handoff/yyyy-mm-dd-m7-complete.md` |

各段完成后按 [`../00-vibe-coding-protocol.md §4`](../00-vibe-coding-protocol.md) 输出完成报告。

## 1. 交付物

> 每条标 `[M7.x]` 关联 §0 子里程碑。完成后把 `[ ]` 替换为 `[x]`。

- [x] `[已存在]` TeleQnA 抽取与转化流水线：`eval/teleqna/` + `eval/builder/`，从公开 [`TeleQnA`](https://github.com/netop-team/TeleQnA) Standards 类 3000 题筛选 + LLM 转化 + 人工校验（M3 已落，119 题入 v1.yaml）
- [x] `[M7.0]` 金标准评测集 `eval/golden/v1.yaml`：2026-05-20 合并落 175 题（119 TeleQnA 转化 + 56 手工补充）；`source==hand_crafted` 切片即 daily 子集
- [x] `[M7.0]` `eval/golden/_template.yaml` 手写题模板（已落，2026-05-19）+ `eval.cli golden validate / merge / stats` 子命令 2026-05-19 落地（44 单测覆盖 validator + merger + stats + 3 套 CLI）
- [x] `[M7.2]` TeleQnA 原生选择题对照评测：`eval/scripts/native_mcq_runner.py`（看 LLM 选对 %，知识准确性维度；2026-05-20 落 mimo-v2.5 + glm-5.1 两模型对照；29 单测覆盖 parse / score / aggregate / mock LLM 全流程；CLI `eval native-mcq run` 触发）
- [x] `[M7.1]` `eval/runner.py`：从金标准集驱动 Agent（HTTP `/chat` SSE）跑出结果，输出 metrics + 报告（2026-05-20 落 `AgentResponse` / `EvalResult` + `consume_sse_stream` + `call_agent` + `compute_eval_metrics` + `run_eval` + `aggregate` + `write_report`；34 单测含 mock-httpx run_eval）
- [x] `[M7.2]` Ragas pipeline：faithfulness / answer_relevance / context_recall / context_precision，judge=`glm-5.1`（2026-05-20 落 `eval/ragas_eval.py` + `eval.runner.run_eval(ragas_scorer=...)` hook；单题异常隔离 + None 占位；ragas / langchain-openai 进 `[project.optional-dependencies] ragas` extras；27 单测含 mock evaluate / NaN / crash / pandas fallback）
- [x] `[已存在]` Telco-DPR 风格 retrieval-only 评测：`eval/runner_retrieval.py`（M3 决胜已用）+ `eval/retrieval/{retriever,metrics,client}.py`
- [x] `[已存在]` Langfuse client + langchain CallbackHandler：`backend/app/agent/langfuse_handler.py`（v4，缺 key 自动 disable）；`.env` 已配 pk/sk/host
- [x] `[M7.3]` Langfuse Dataset：`eval/langfuse_dataset.py` push 金标准 + runner 每次跑上传 score（2026-05-20 落 `push_golden_to_langfuse` + `push_run_score` + `make_eval_trace_id` + 单例 `get_client`；`run_eval(langfuse_run_label=..., langfuse_dataset_name=...)` 一处启用；缺 key 自动 disable，runner 主路径不变；21 单测含 mock SDK / 缺 key / 单条失败隔离 / runner 集成）
- [x] `[已存在]` `ApiUsage` 表 + Alembic 迁移 + `/admin/stats` 7 天聚合查询（M4.10）
- [ ] `[M7.4]` 成本与用量监控**写入链路**：`services/usage.py` + `llm/pricing.py` + `services/alerts.py`（仅 log warning）+ LiteLLM 响应 `usage` 钩
- [x] `[已存在]` Pytest `eval` marker（`pyproject.toml::markers` + `Makefile::eval`）+ `ragas>=0.2` 已声明
- [x] `[M7.1]` `backend/tests/eval/test_golden_v1.py`：D13 第一档（宽松）阈值断言；smoke（canned graph，always run）+ daily / full（`RUN_LIVE_EVAL=1` 触发；2026-05-20）+ Makefile `eval-daily` / `eval-weekly` target
- [x] `[M7.1]` `backend/app/agent/not_found_phrases.py` + `eval/not_found_phrases.py`（镜像）：双语短语词表 + `is_not_found_answer()`；供 agent + eval runner 共享导入（42 单测含 mirror 同步校验）
- [x] `[M7.1]` `eval/sse_parser.py`：`SSEEvent` / `parse_sse_text` / `SSEStreamParser`（13 单测覆盖一次/流式/ping/EOF/JSON 校验）
- [ ] `[M7.6]` Daily / Weekly CI：`.github/workflows/eval-{daily,weekly}.yml`；阈值未达自动开 GitHub issue

## 2. 评测体系总览

```mermaid
flowchart TB
    subgraph build["金标准构建一次性"]
        T1["TeleQnA repo (10k 题)"] --> T2["filter: Standards specifications + Standards overview (3000 题)"]
        T2 --> T3["按 Rel-18/19 相关性 / 实体匹配筛选<br/>(100-200 题)"]
        T3 --> T4["LLM 转化: MCQ -> 开放问答 + 期望事实点"]
        T4 --> T5["人工校验修改"]
        H1["手工补充 20-30 题<br/>(表格/公式/多章节合并/负样本)"]
        T5 & H1 --> G["eval/golden/v1.yaml"]
    end
    subgraph eval["三路评测"]
        G --> R["eval/runner.py<br/>(开放问答 RAG eval)"]
        T2 --> NR["eval/teleqna/native_mcq_runner.py<br/>(选择题对照)"]
        E1["eval/embedding_poc.py<br/>(retrieval-only top-K/MRR)"]
        R --> RG["Ragas: faith/rel/recall/prec"]
        R --> LF["Langfuse Datasets + Scores"]
        R --> REP["报告 markdown + json"]
        NR --> REP
        E1 --> REP
    end
    A["backend agent (HTTP /chat)"]
    Q["Qdrant + BM25"]
    R --> A
    A --> Q
    A --> LF
```

## 3. 金标准集构建工作流

### 3.1 TeleQnA 拉取与过滤

```python
# eval/teleqna/pull.py
# 1. git clone https://github.com/netop-team/TeleQnA
# 2. 解压 TeleQnA.txt.zip (密码 'teleqnadataset')
# 3. 解析为 list[dict]
# 4. filter category in {"Standards specifications", "Standards overview"}  → 3000 题
# 5. 进一步过滤与 Rel-18/19 相关：
#    - 关键词匹配（NR / 5GS / SBA / NEF / NWDAF / AMF/SMF/UPF / ...）
#    - LLM 二次确认（mimo-v2.5 判定 yes/no）
#    保留 200-300 题
```

输出：`eval/teleqna/filtered.jsonl`，结构：

```json
{
  "id": "teleqna-2456",
  "category": "Standards specifications",
  "question": "Which of the following is responsible for ... ?",
  "options": {"option 1": "AMF", "option 2": "SMF", "option 3": "UPF", "option 4": "NEF"},
  "answer": "option 2: SMF",
  "explanation": "...",
  "filter_score": 0.92
}
```

### 3.2 选择题 → 开放问答转化

```python
# eval/builder/transform.py
# 对每个 filtered TeleQnA item：
# 用 LLM (glm-5.1) 生成：
# - rewritten_question: 把"以下哪个..."这种 MCQ 题面改为开放式提问
# - expected_specs: 根据 explanation 推断哪几篇 spec 涉及
# - expected_facts: 从 answer + explanation 抽取关键事实
# - candidate_section_hints: 从 explanation 提取可能的章节关键词
```

输出：`eval/golden/v1.draft.yaml`（v1 草稿，待人工校验）

**LLM 转化 prompt 要点**：

```
你将一道 telecom 选择题转化为 RAG 评测题目。

原题：{question}
选项：{options}
正确答案：{correct_option}
解释：{explanation}

任务：
1. 改写为开放式问题（不要泄露选项；保留 telecom 术语）
2. 给出预期 spec_id 列表（从解释中推断；保守只给确定的）
3. 给出 3-7 个"答案必须命中的关键事实"（substring 即可）
4. 给出 1-3 个"答案不能包含的内容"（避免幻觉）

输出 YAML：
```

### 3.3 人工校验流程

简单脚本 `eval/builder/review.py`：

- 在终端按 q 顺序展示原 TeleQnA + 转化结果
- 操作：`a` accept / `e` edit (开 EDITOR) / `r` reject / `s` skip
- accept 写入 `eval/golden/v1.yaml`，reject 写入 `eval/golden/_rejected.yaml`

**目标**：100 题转化后人工通过 ≥ 80 题；剔除"原 TeleQnA 答案存疑"的题目。

### 3.4 手工补充（20-30 题）

补充 TeleQnA 难以覆盖的场景：

- **表格定位**："38.331 中 RRCReconfiguration 的 IE 列表完整结构"
- **公式查询**："38.214 的 CQI 计算公式"
- **章节路径**："列出 23.502 §4.2 所有子节"
- **多章节合并推理**："列出 23.502 §4.3 PDU Session 建立涉及到的所有 NF 与消息序列"
- **负样本**："5G UE 的 MAC 地址格式"（必须返回未找到）

每条保持 §3.5 的 YAML 格式。

### 3.5 金标准集格式

`eval/golden/v1.yaml`：

```yaml
version: 1
created_at: 2026-05-20
total: 120                # 100 TeleQnA 转化 + 20 手工补充
sources:
  - teleqna_transformed   # 来源标识
  - hand_crafted

categories:                # 2026-05-19 砍 tool；10 个名额分到 multi_section+2/formula+4/negative+4
  - definition            # ~30 题
  - procedure             # ~35 题
  - multi_section         # ~12 题（多章节合并推理，但不跨 spec/版本）
  - table_lookup          # ~10 题
  - formula               # ~14 题
  - negative              # ~19 题 (期望"未找到")

items:
  - id: def-001
    source: teleqna_transformed
    teleqna_origin_id: "teleqna-2456"
    category: definition
    language: en
    question: "What is the definition of 'PDU Session' in 5G System?"
    expected_specs:
      - spec_id: "23.501"
        sections:
          - "3.1"            # 章节路径前缀，匹配即可
    expected_facts:           # 关键事实点（substring 或 regex 任一命中即算覆盖）
      - "association between"
      - "UE and a DN"
    forbidden:                # 答案不能包含的内容（用于检测幻觉）
      - "4G"
    notes: "PDU Session 是 5G 核心概念"

  - id: proc-005
    category: procedure
    language: zh
    question: "请描述 5G UE Initial Registration 完整流程"
    expected_specs:
      - spec_id: "23.502"
        sections: ["4.2.2"]
    expected_facts:
      - "Registration Request"
      - "AMF selection"
      - "AUSF"
      - "UDM"
      - "Registration Accept"
    expected_min_facts_coverage: 0.7   # 至少命中 70%

  - id: neg-002
    category: negative
    language: en
    question: "What is the MAC address format of a UE PDU Session?"
    expected_specs: []
    expected_facts: []
    must_say_not_found: true           # 答案必须明示"未找到"
```

**维护规则**：

- TeleQnA 转化的题目必须保留 `teleqna_origin_id` 便于回溯
- 手工题与转化题在统计上同等权重；CI eval 子集时分层抽样
- `expected_facts` 是 "答案里必须出现的关键事实"，不要 paraphrase 一致才算
- `must_say_not_found` 给负样本做严格 grounding 校验
- CI 子集必须按 category 分层抽样，至少覆盖 definition / procedure / table_lookup / negative；不得只抽简单题。
- **建议问题区不进评测**（2026-05-20）：触发 `must_say_not_found` 的回答下方可能由 agent `suggest_questions` 节点附加"你想问的是不是"超链接建议区（详见 [`../04-handoff/2026-05-20-suggested-questions.md`](../04-handoff/2026-05-20-suggested-questions.md)）；该建议区有/无、命中数都**不参与**任何 metric（`fact_coverage` / `forbidden_violations` / `must_say_not_found_passed` / Ragas 全部排除其影响）。建议区文本是否触发 `forbidden` 命中仍按主回答规则扫，是已有规则的自然延伸，不算新指标

### 3.6 重跑 SOP（2026-05-19 落 CLI 后可一键重跑）

如果 TeleQnA 上游更新 / 想换 LLM 模型 / 想重做 transform，按下序重跑（每步可独立）：

```bash
# 0. 进 eval 目录，依赖已通过 uv sync
cd /data/3GPP-Everything && uv sync --project eval --extra dev

# 1. 拉 TeleQnA 仓库 → 解压 (AES) → parse JSON → raw.jsonl
#    输出：eval/teleqna/data/raw.jsonl (~10k 行)
uv run --project eval python -m eval.cli teleqna pull

# 2. raw.jsonl → filtered.jsonl + out_of_scope.jsonl + stats
#    硬约束：17 篇 whitelist；--keep-overview/--strict 二选一
#    输出：eval/teleqna/data/filtered/{filtered,out_of_scope}.jsonl + stats.json
uv run --project eval python -m eval.cli teleqna filter

# 3. (可选) 对没硬命中 whitelist 的 Standards 类题跑 LLM 推断
#    输出：eval/teleqna/data/llm_inferred.jsonl
uv run --project eval python -m eval.cli teleqna infer --rpm 50 --concurrent 8

# 4. MCQ → 开放问答 LLM 转化（mimo-v2.5-pro）
#    输入：filtered.jsonl OR llm_inferred.jsonl（自动识别）
#    输出：eval/golden/v1.draft.yaml
uv run --project eval python -m eval.cli builder transform \
    --candidates eval/teleqna/data/filtered/filtered.jsonl \
    --min-confidence medium

# 5. 草稿先 validate（schema 通过才进人工校验）
uv run --project eval python -m eval.cli golden validate -f eval/golden/v1.draft.yaml

# 6. 人工校验（accept / edit / reject）→ 写入 eval/golden/v1.yaml
#    review 脚本本期仍为半自动；编辑直接打 v1.yaml 也行
#    （`eval/builder/review.py` 在 §3.3 列出，命令以最终落地为准）

# 7. (可选) 手写题与 teleqna 转化 merge
uv run --project eval python -m eval.cli golden merge \
    -i eval/golden/v1.yaml \
    -i eval/golden/v1.handwritten.yaml \
    -o eval/golden/v1.yaml

# 8. 最终 validate + 看分布
uv run --project eval python -m eval.cli golden validate -f eval/golden/v1.yaml
uv run --project eval python -m eval.cli golden stats    -f eval/golden/v1.yaml
```

**SOP 自检（可在 CI 跑）**：
- 步骤 1-3 涉及网络 / LLM 调用 → 不进 unit；纯函数已在 `eval/tests/unit/` 覆盖（`test_filter.py` / `test_infer.py` / `test_builder.py`）
- 步骤 4-8 任一改动 → 必须重跑步骤 5 + 8（validate / stats），把这两条作为门禁

> 上一次完整重跑：M3 决胜（2026-05-16，119 题落 v1.yaml）。如需在 M7 重跑，先和人确认是
> 否要换 transform LLM（影响 expected_facts 风格 → 既有人审结果失效）。

## 4. Runner 实现

> **实装状态**（2026-05-20，M7.1 完成）：`eval/runner.py` 已落；下方代码与实际签名一致；
> Ragas / Langfuse hook 留 `None` 占位，由 M7.2 / M7.3 各自 PR 填。

`eval/runner.py` 关键 dataclass + 入口：

```python
@dataclass(slots=True)
class AgentResponse:
    """从 SSE 流还原的 agent 终态。"""
    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    chunks_hit: list[dict] = field(default_factory=list)
    chunks_rerank: list[dict] = field(default_factory=list)
    node_durations_ms: dict[str, int] = field(default_factory=dict)
    terminal_event: str = "incomplete"   # final | cancelled | error | end | http_error
    error: dict | None = None
    duration_ms: int = 0
    token_event_count: int = 0


@dataclass(slots=True)
class EvalResult:
    item_id: str
    category: str
    language: str
    # retrieval（negative 题 expected_specs=[] → None，aggregate 用 _safe_mean 跳过）
    retrieved_specs: list[str]
    retrieved_sections: list[str]
    context_recall_spec: float | None
    context_recall_section: float | None
    # 答案
    answer: str
    citations: list[dict]
    fact_coverage: float | None
    forbidden_violations: list[str]
    must_say_not_found_passed: bool | None
    # Ragas（M7.2 填，目前 None 占位）
    ragas_faithfulness: float | None = None
    ragas_answer_relevance: float | None = None
    ragas_context_recall: float | None = None
    ragas_context_precision: float | None = None
    # 性能（llm_calls / cost 由 M7.4 usage hook 后填）
    duration_ms: int = 0
    llm_calls: int = 0
    total_cost_usd: float = 0.0
    # bookkeeping
    terminal_event: str = ""
    error: dict | None = None


async def run_eval(
    golden_path: Path,
    *,
    client: httpx.AsyncClient,     # 真 HTTP 或 ASGITransport in-process
    auth_token: str,                # JWT bearer
    source_filter: str | None = None,
    subset: int | None = None,
    mode: str = "qa",
    api_prefix: str = "/api/v1",
) -> list[EvalResult]:
    """对 golden items 顺序跑端到端评测。

    流程：POST /api/v1/sessions → POST /api/v1/sessions/{sid}/messages → SSE →
    consume_sse_stream → compute_eval_metrics。单题 HTTP 异常隔离（terminal_event="http_error"）。
    """
```

调用方负责传 `httpx.AsyncClient`（真实部署：base_url=后端地址；测试：`ASGITransport(app=app)`）
+ 已登录 bearer token。每题一个独立 session（避免历史污染 retrieval）。

模块边界 / 单测拆点：

- `consume_sse_stream(line_iter)`：纯函数，喂行流 → `AgentResponse`；终止事件
  (`final` / `cancelled` / `error` / `end`) 决定 `terminal_event`
- `compute_eval_metrics(item, resp)`：纯函数。指标口径见 §4.1
- `call_agent(...)`：开 session + 发 message + 消费 SSE
- `run_eval(...)`：顶层 orchestrator（顺序执行，M7.6 看耗时再加并发）
- `aggregate(results)` / `write_report(results, outdir)`：报告输出

### 4.1 指标实现口径

| 字段 | 实现 |
|---|---|
| `context_recall_spec` | `1.0 if any` over `chunks_rerank → chunks_hit → citations`（与 `eval/retrieval/metrics.py::is_spec_hit` 一致）；空 `expected_specs` → `None` |
| `context_recall_section` | 同上但用 `is_section_hit`（`.` 切段做章节前缀匹配）|
| `fact_coverage` | substring case-insensitive 命中率；空 list → `None` |
| `forbidden_violations` | substring case-insensitive 命中字符串数组 |
| `must_say_not_found_passed` | `is_not_found_answer(answer, lang) AND not forbidden_violations`；仅 negative；en/zh 词表切换；命中 forbidden = `False` |
| `duration_ms` | `time.perf_counter()` 从 POST /messages 起，到 SSE 流结束 |

输出：

- `eval-results/{timestamp}/report.md`（人读，含 aggregate + 异常题清单）
- `eval-results/{timestamp}/results.json`（机器读，含 `aggregate` + 每条 `asdict(EvalResult)`）
- Langfuse Cloud trace + score（M7.3 已接通；调用方传 `langfuse_run_label="..."` 启用；缺 key 自动 disable，runner 仍跑）

## 5. Ragas 集成

```python
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_recall, context_precision
from datasets import Dataset

ds = Dataset.from_list([
    {
        "question": r.item.question,
        "answer": r.answer,
        "contexts": [c.content for c in r.contexts],
        "ground_truth": r.item.expected_facts_joined,
    }
    for r in results
])
ragas_scores = evaluate(ds, metrics=[faithfulness, answer_relevancy, context_recall, context_precision])
```

**Ragas 用的 LLM**（评估时本身也要调 LLM）：建议**用与 Agent 不同**的模型避免同源偏差。例如 Agent 用 `mimo-v2.5-pro`，Ragas 评估用 `glm-5.1`（都在 LiteLLM 中）。

```python
import os
os.environ["RAGAS_LLM"] = "langchain_openai.ChatOpenAI"
ragas_llm = ChatOpenAI(model="glm-5.1", base_url=LITELLM_BASE, api_key=LITELLM_KEY)
ragas_embed = ... # 同 RAG 用的 embedding，或独立的
```

## 6. Langfuse Datasets（M7.3 实装）

> 模块 `eval/langfuse_dataset.py`；EvalSettings 加 `langfuse_public_key / secret_key / host`（顶层 `.env` 已配，eval 子项目自动读取）。
> 缺任一 key → `get_client()` 返回 `None`，所有公开函数 short-circuit 返回 0；runner 主路径不受影响。

### 6.1 一次性推送 dataset

```python
from pathlib import Path
from eval.langfuse_dataset import push_golden_to_langfuse

n = push_golden_to_langfuse(
    Path("eval/golden/v1.yaml"),
    dataset_name="tgpp-golden-v1",
)  # 返回成功 upsert 的 item 数
```

幂等性：`create_dataset_item(id=<GoldenItem.id>, ...)` SDK 文档保证 "Upserts if an item with id already exists"；二次推送会按相同 id 覆盖 input / expected_output / metadata。
单条 item 写失败（网络抖动 / 单 id 校验失败）只 `log.warning`，不阻塞其他题。

每个 dataset item 字段映射：

| Langfuse 字段 | GoldenItem 来源 |
|---|---|
| `id` | `item.id`（幂等键） |
| `input.question/category/language` | 同名字段 |
| `expected_output.expected_facts/expected_specs/forbidden/must_say_not_found` | 同名字段 |
| `metadata.source/teleqna_origin_id/notes` | 同名字段（便于 Cloud UI 按 source 筛 hand_crafted vs teleqna_transformed） |

### 6.2 runner 跑 eval 时上传 trace + score

```python
# eval/runner.py::run_eval(...)
results = await run_eval(
    golden_path,
    client=httpx_client,
    auth_token=token,
    langfuse_run_label="m7-daily-2026-05-20",   # 启用 langfuse 路径
    langfuse_dataset_name="tgpp-golden-v1",     # 仅 metadata
)
```

每条 item 的内部流程（只在启用且 `get_client()` 可用时执行）：

1. `make_eval_trace_id(run_label, item.id)` → 用 `client.create_trace_id(seed="m7-daily-2026-05-20:def-001")` 得到 32 字符幂等 trace_id（同一 (label, id) 多次跑 → 同一 trace_id）
2. `client.create_event(name="eval-item-<id>", trace_context={"trace_id": trace_id}, input={question,category,language}, output={answer,terminal_event,citations}, metadata={item_id,source,dataset,duration_ms})` —— 让 Cloud UI 看到 IO，evaluator 可读
3. `push_run_score(trace_id, result_score_dict, ...)` —— 把 9 个 metric 全部按 NUMERIC 上传：
   - `context_recall_section` / `context_recall_spec` / `fact_coverage`
   - `must_say_not_found_passed`（bool → 0/1）
   - `forbidden_violation`（0/1，命中 forbidden = 1）
   - `ragas_faithfulness` / `ragas_answer_relevance` / `ragas_context_recall` / `ragas_context_precision`（Ragas 启用时填）
4. 每个 metric 写失败只 log warning + skip 该 metric，其他正常；`None / NaN` 自动 skip

`EvalResult` 多一个 `langfuse_trace_id: str | None` 字段，落 `results.json` 方便事后追到对应 trace。

### 6.3 Langfuse 自动 eval（Cloud 内置）

`[human]` 配置（M7.3 验收的最后一步）：

- 在 Cloud UI 中开启 Dataset `tgpp-golden-v1` 关联的 built-in evaluators（`faithfulness`、`relevance`）
- 跑完一个 run → Cloud 自动按 trace input/output 算分；runner 上传的 score 与 Cloud 算的 score 并列在 UI

### 6.4 故障模式

| 现象 | 行为 |
|---|---|
| `.env` 缺 `LANGFUSE_*` key | `get_client()` 返回 None；`push_*` 函数返回 0；runner 跑得通，`results[i].langfuse_trace_id` = None |
| `langfuse` SDK 未装（pip 失败 / 升级中） | import 异常被吞，等同缺 key |
| Cloud 网络抖动 / 单条 score 写失败 | log.warning，跳过该 metric / item，不挂 runner |
| 二次推送同 dataset | `create_dataset` 抛 409 被吞；`create_dataset_item(id=...)` 按 SDK 文档 upsert |

## 7. Pytest 集成

> **D13 两档阈值（2026-05-18 人审通过 Q1 决策）**：M7 nightly 用宽松版（`test_golden_v1_subset` 当前
> 实装），用于尽早暴露 retrieval / agent 质量问题；M8 上线门槛用严格版（`test_golden_v1_full` 当前实装），
> 仅在上线前 PR 收紧（详见 `04-handoff/2026-05-18-tech-debt-cleanup-todo.md` Q1 与 batch C / D）。
>
> | 档位 | 触发时机 | faithfulness | context recall | answer relevancy | answer correctness | latency-p50 | cost-p50 |
> |---|---|---|---|---|---|---|---|
> | **宽松（M7 nightly）** | M7 启动后每日 | ≥ 0.75 | ≥ 0.65 | ≥ 0.70 | ≥ 0.55 | ≤ 6s | ≤ ¥0.30 |
> | **严格（M8 上线门槛）** | M8 上线前 PR | ≥ 0.85 | ≥ 0.80 | （收紧 PR 时定）| （收紧 PR 时定）| （同上）| （同上）|
>
> 实施位置（2026-05-19 决策 Q1）：宽松版断言在 `test_golden_v1_daily`（每日跑 `source==hand_crafted` 切片，≥ 20 题）；
> 严格版断言在 `test_golden_v1_full`（每周一全集 ≥ 140 题）；M7 → M8 之间一次性 PR 把严格版
> 写进 `test_golden_v1_full` 的最终断言（不破坏 daily / weekly）。

**`backend/tests/eval/test_golden_v1.py`**（2026-05-20 实装；与原 sketch 略有调整）：

三个用例分工：

1. **`test_runner_smoke_against_canned_backend`**（@pytest.mark.eval，always run）：
   注入 `_CannedGraph` + ASGITransport in-process backend，对 1 道临时金标准题跑
   `run_eval`，断言 `EvalResult` 各字段。**不调真实 LLM**。作用：backend SSE event
   名 / 字段一旦漂移，本测试 fail；这是 runner ↔ backend 契约的自动哨兵
2. **`test_golden_v1_daily`**（@pytest.mark.eval + skipif）：D13 宽松档；需
   `RUN_LIVE_EVAL=1` + `EVAL_BACKEND_BASE_URL` + `EVAL_BACKEND_TOKEN` 才触发
3. **`test_golden_v1_full`**（同上）：每周一全集；M7 期间用宽松，M8 上线前 PR 改严格

```python
import os
import pytest

_RUN_LIVE = os.getenv("RUN_LIVE_EVAL") == "1"

@pytest.mark.eval
@pytest.mark.skipif(not _RUN_LIVE, reason="需 RUN_LIVE_EVAL=1 + 真 backend")
async def test_golden_v1_daily() -> None:
    """每日 CI - daily 子集（source==hand_crafted，≥ 20 题，D13 宽松档）"""
    base_url = os.environ.get("EVAL_BACKEND_BASE_URL", "http://localhost:8000")
    token = os.environ["EVAL_BACKEND_TOKEN"]
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        results = await run_eval(
            GOLDEN_V1,
            client=client,
            auth_token=token,
            source_filter="hand_crafted",
        )
    assert len(results) >= 20, "daily 子集题数不足 20"
    recalls = [r.context_recall_section for r in results if r.context_recall_section is not None]
    avg_recall = mean(recalls)
    assert avg_recall >= 0.65, f"context recall too low: {avg_recall}"
    # 负样本必须 100% 触发 not_found（forbidden 命中 = 失败）
    neg = [r for r in results if r.category == "negative"]
    neg_passed = [r for r in neg if r.must_say_not_found_passed]
    assert len(neg_passed) == len(neg), \
        f"negative 未全过：{len(neg_passed)}/{len(neg)}"

@pytest.mark.eval
@pytest.mark.skipif(not _RUN_LIVE, reason="需 RUN_LIVE_EVAL=1 + 真 backend")
async def test_golden_v1_full() -> None:
    """每周一 CI - 全集 ≥ 140 题（M7 用宽松 ≥ 0.65；M8 上线前 PR 改 ≥ 0.80）"""
    ...
```

> **与原 sketch 的差异**：
> - `run_eval` 改为关键字参数（`client` + `auth_token` 必填，移除 `api_client` fixture）
> - daily 实装暂不断 ragas_faithfulness（M7.2 接通后补）
> - daily/full 均加 `skipif RUN_LIVE_EVAL` gate，避免在无 backend 环境下错误失败
> - 增加 smoke 用例守 runner↔backend 契约
>
> **Makefile target**（2026-05-20 落）：
>
> ```
> make eval         # 所有 eval marker（smoke + daily/full 含 skip）
> make eval-daily   # pytest -m eval -k "daily or smoke"
> make eval-weekly  # pytest -m eval -k "full or smoke"
> ```

## 8. Embedding 维度决胜评测（2026-05-16 修订 → ✅ 决胜完成）

> **决策变更**：放弃原"voyage / 智谱 embedding-3 双轨决胜"，改为"voyage 单轨 + 2048/1024 维度 ablation"。
> 详见 [`docs/02-tech-selection.md §3.1`](../02-tech-selection.md#31-选型决策2026-05-16) 与
> [`docs/03-development/02-ingestion-and-indexing.md §4.7`](02-ingestion-and-indexing.md#47-poc-验证步骤修订)。
> 智谱 `embedding-3` 仅保留代码层 fallback，不进入决胜评测。
>
> **✅ 决胜结果（2026-05-16）：`1024` 胜出**。所有指标差距 ≤ 2pp，触发 tie-fallback；
> 1024 在 119 题金标准上全线略胜或持平（spec R@10 0.815 vs 0.798；R@10 0.647 vs 0.630；table_lookup 类 +8.4pp）。
> 报告 + 签字记录：[`eval-results/m3-embedding-poc.md`](../../eval-results/m3-embedding-poc.md)。
> 2048 collection 已 drop，生产维度固化为 1024。

这是 M3 关键决策点。专用脚本 `eval/embedding_poc.py`：

```python
async def main():
    # 1. 两个 collection 已就绪 (M2 完成):
    #    tgpp_chunks_voyage_d2048 / tgpp_chunks_voyage_d1024
    #    （voyage MRL 性质让一次 API 调用同时产 2048 + 1024 维向量）
    # 2. 关掉 Agent 上层，仅评 retrieval-only
    items = load_golden("eval/golden/v1.yaml")
    for dim in [2048, 1024]:
        recall_at_5, recall_at_10, recall_at_20, mrr = [], [], [], []
        for it in items:
            hits = await retrieve_only(
                it.question, provider="voyage", dim=dim, top_k=20
            )
            r5 = compute_section_recall(hits[:5], it.expected_specs)
            r10 = compute_section_recall(hits[:10], it.expected_specs)
            r20 = compute_section_recall(hits[:20], it.expected_specs)
            mrr.append(compute_mrr(hits, it.expected_specs))
            ...
        print(f"voyage_d{dim} | R@5={mean(r5):.2f} R@10={mean(r10):.2f} "
              f"R@20={mean(r20):.2f} MRR={mean(mrr):.2f}")
```

**决胜规则**（写在文档与 README）：

- R@10 差距 > 2% → 选 R@10 高者
- 否则比 MRR；MRR 差距 > 2% → 选高者
- 否则差距不显著 → 选 **1024 维**（存储省一半、检索 latency 快 30-50%、HNSW 内存占用更友好）

结果一并 push 到 Langfuse 与 git 一个 `eval-results/m3-embedding-poc.md` 记录决策。决胜后立即 drop 输者 Qdrant collection（**已于 2026-05-16 完成：drop `tgpp_chunks_voyage_d2048`**）。

**M3 → M6 过渡硬指标**（2026-05-16 新增）：

- 决胜后若任何 chunker / vision 改动会让 content 变化（影响 chunk_id），必须在 20 篇 POC 上重跑改动后的 chunker → diff chunk_id 集合
- **漂移率 > 5% 视为"chunker 未稳定"**，禁止进入 M6 全量索引；先在 20 篇上 ablation 确认指标改善才能上 M6
- 漂移率 ≤ 5% 时 M6 可通过 `--skip-indexed` 跳过 POC 20 篇，省 ~8M voyage tokens

## 9. 成本与用量监控

### 9.1 计费层

`backend/app/services/usage.py`：

- LLM / Embedding / Reranker / Vision / WebSearch 每次调用计入 PG `api_usage`
- LLM token 数从 LiteLLM 响应 `usage` 字段读
- Embedding 按 token 数估算
- Reranker 按 token 数计费（Voyage 口径：`query_tokens × n_docs + Σ doc_tokens`，**不是按 query 次数**）
- WebSearch 按调用次数计费
- 单价由 `app/llm/pricing.py` 表维护；标的是"用尽免费额度后"的等效单价，免费区内本表算出的成本由 usage 上层标记为 `billed=false`

```python
PRICING = {
    "mimo-v2.5-pro":    {"input": 1.0/1e6, "output": 3.0/1e6},
    "mimo-v2.5":        {"input": 0.4/1e6, "output": 2.0/1e6},
    "voyage-4-large":   {"per_token_embed": 0.12/1e6},      # 200M tokens 免费
    "voyage-rerank-2.5": {"per_token_rerank": 0.05/1e6},    # 200M tokens 免费，按 token 不按 query
    "tavily-search":    {"per_call": 0.01},
}
```

### 9.2 告警阈值

`backend/app/services/alerts.py`：

- 每日聚合 job（apscheduler 或 cron）：检查 `api_usage(day=today)`
- 阈值（可在 .env 覆盖）：
  - 日总成本 > $5 → log warning
  - 日总成本 > $10 → 发邮件 / Telegram / Discord webhook（看用户偏好）
  - 月累计 > $50 → 同上

### 9.3 前端展示

管理后台 `usage_panel` 展示：

- 折线：最近 30 天 daily cost
- 饼图：今日成本分项（LLM / embed / rerank / web）
- 数字：本月累计 / 本月查询数

## 10. Langfuse 配置清单

需要在 Langfuse Cloud 上手工做的：

- [x] 注册账号
- [x] 新建 project `tgpp-everything`
- [x] 拿 public_key + secret_key → 写 `.env`（`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`，2026-05-19 前已配）
- [x] 创建 Dataset `tgpp-golden-v1` 与首次推送：由 `eval.langfuse_dataset.push_golden_to_langfuse()` 程序化执行（2026-05-20 M7.3 落地；M7.2 评估结束后人触发 `python -c "from pathlib import Path; from eval.langfuse_dataset import push_golden_to_langfuse; print(push_golden_to_langfuse(Path('eval/golden/v1.yaml')))"`）
- [ ] `[human]` 启用内置 evaluators（faithfulness、relevance）关联到 Dataset（在 Cloud UI Dataset 页面操作）
- [ ] 设置成本预警（Free Tier 含基本告警）

## 11. 监控指标（应用层）

记录到 PG / structlog（小规模多用户阶段先不引入 Prometheus）：

- `agent.run.duration_ms` (p50/p95)
- `agent.run.llm_calls`
- `agent.run.error_rate`
- `agent.node.duration_ms` by node
- `retrieve.recall_at_5` (from eval runs)
- `db.connection.errors`
- `litellm.errors_by_model`

二期可外挂 OpenTelemetry。

## 12. 验收清单

> 按 §0 子里程碑分组。标注：`[auto]` = Agent 自跑可判定；`[human]` = 需要人介入（评测内容由懂 3GPP 的人 review、外部账号、决策签字）。同一段全绿才能进下一段。

### M7.0 金标准 v1 → v1.5

- [x] `[已落]` `eval/golden/_template.yaml` 手写题模板（4 个示例：negative / formula×2 / multi_section，2026-05-19；同日砍 tool category）
- [x] `[auto]` `eval.cli golden validate --file <yaml>` 子命令：必填字段 / 枚举值 / id 唯一性 / language 取值校验，错误位置精确报行（2026-05-19 落 `eval/validators/golden.py` + 22 单测；含 `--json` / `--strict-warnings` 选项）
- [x] `[auto]` `eval.cli golden merge` 子命令：把 `v1.handwritten.yaml` 合并到 `v1.yaml`，跨文件检查 0 重复 id（2026-05-19 落 `eval/validators/merger.py` + 11 单测；含 `--dry-run` / `--force` 选项）
- [x] `[auto]` TeleQnA 拉取 + 过滤 + 转化流水线可重跑：`eval.cli teleqna {pull,filter,infer}` + `eval.cli builder transform` 在 M3 已就位；2026-05-19 在 §3.6 落 SOP 文档
- [x] `[auto]` `eval/golden/v1.yaml` 题数 ≥ 140 题（2026-05-20 合并落 175 题；含 `teleqna_origin_id` 可追溯）；hand_crafted 56 ≥ 20 已达；M7.0 完成报告：[`../04-handoff/2026-05-20-m7.0-complete.md`](../04-handoff/2026-05-20-m7.0-complete.md)
- [ ] `[human]` **至少 20 题（手写部分）由懂 3GPP 的人 review 过**（这是质量门禁，Agent 不能自己说通过；2026-05-20 待办）
- [x] `[auto]` 分布按 §3.4 容差 ±5 题校验：`eval.cli golden stats -f <yaml>` 输出 category / source / language 分布 + 目标 ±5 容差比对；2026-05-19 落 `eval/validators/stats.py` + 11 单测（实际分布达标仍依赖人写题）。**2026-05-19 砍 tool category**：目标 multi_section 12 / formula 14 / negative 19 / 其余不变，合计 120

### M7.1 端到端 runner + 第一档阈值 ✅ 2026-05-20

> M7.1 完成报告：[`../04-handoff/2026-05-20-m7.1-complete.md`](../04-handoff/2026-05-20-m7.1-complete.md)

- [x] `[auto]` `eval/runner.py`：HTTP `POST /api/v1/sessions/{sid}/messages` 取 SSE → 拼 `answer` + `citations` → 计算 `fact_coverage` / `forbidden_violations` / `must_say_not_found_passed` / `context_recall_section` / `context_recall_spec`（2026-05-20 落）
  - **`must_say_not_found_passed` 判定双语**（2026-05-19 补）：按题目 `language` 字段切词表。en 至少覆盖 `not found` / `not specified` / `no such` / `does not define` / `is not defined in` / `outside the scope`；zh 至少覆盖 `未找到` / `未定义` / `规范未规定` / `不涉及` / `不在范围内` / `没有相关规定`
  - **词表单点定义**（2026-05-20 落）：双语短语在 `backend/app/agent/not_found_phrases.py`（`NOT_FOUND_PHRASES_EN` / `NOT_FOUND_PHRASES_ZH` + `is_not_found_answer()`）；eval 不依赖 backend → `eval/not_found_phrases.py` 镜像 + `test_mirror_with_backend_module` 单测强制同步（详见 [`../04-handoff/2026-05-20-suggested-questions.md`](../04-handoff/2026-05-20-suggested-questions.md) §3.1）
- [x] `[auto]` 输出 `eval-results/{ts}/{report.md, results.json}`：`run_eval` 完事调 `write_report(results, outdir)`；`aggregate` 报告聚合 + 异常题清单（2026-05-20 落）
- [x] `[auto]` runner 单测：mock httpx `MockTransport` SSE 流 → 断言 metrics 计算正确（34 case 含 happy / source_filter+subset / HTTP 500 单题隔离）
- [x] `[auto]` `backend/tests/eval/test_golden_v1.py` 落 D13 第一档断言（context recall ≥ 0.65 / 负样本 100% 过 `must_say_not_found_passed`）；smoke（canned graph，always run）+ daily / full（`RUN_LIVE_EVAL=1` gate）
- [x] `[auto]` Makefile `eval-daily` / `eval-weekly` target：`pytest -m eval -k "daily or smoke" / "full or smoke"`（2026-05-20 落）
- [ ] `[M7.6 CI]` daily 子集（`source==hand_crafted`，≥ 20 题）< 10min 全绿；负样本必须全过 `must_say_not_found_passed`（需 `RUN_LIVE_EVAL=1` + 真 backend；M7.6 接通 CI）
- [x] `[M7.2]` ragas_faithfulness / answer_relevancy / context_recall / context_precision 字段：runner 留 `None` 占位，M7.2 补（2026-05-20 通过 `eval.runner.run_eval(ragas_scorer=...)` 注入 `RagasScorer` 即填；CI / pytest live 链路按需启用，默认 None 不影响 mock 测试）

### M7.2 Ragas + native MCQ ✅ 2026-05-20

> M7.2 完成报告：[`../04-handoff/2026-05-20-m7.2-complete.md`](../04-handoff/2026-05-20-m7.2-complete.md)

- [x] `[auto]` Ragas 4 metric 接入：faithfulness / answer_relevancy / context_recall / context_precision；judge LLM = `glm-5.1`（temperature=0）；评估 embedding 复用 `voyage-4-large`（2026-05-20 落 `eval/ragas_eval.py::build_default_ragas_scorer` + `RagasScorer.score_item`；4 metric 字段 `ragas_faithfulness` / `ragas_answer_relevance` / `ragas_context_recall` / `ragas_context_precision` 写回 `EvalResult`）
- [x] `[auto]` Ragas 单题失败容忍：单条评估异常不挂 runner（log warning + 该 metric 记 None）：`score_item` 内部 try/except `ragas.evaluate(...)` 任一异常 / NaN / 缺 metric key / 空 contexts/answer 都退化为全 None；`run_eval` 外层再兜底 `RagasScorer.score_item` 自身 crash
- [x] `[auto]` `eval/scripts/native_mcq_runner.py`：从 TeleQnA filtered.jsonl 跑选择题对照（mimo-v2.5 + glm-5.1 各一遍），输出 LLM 选对 % 报告归档 `eval-results/m7-native-mcq/{ts}/report.md`（2026-05-20 落 + `eval native-mcq run` CLI；每模型独立 ModelAggregate 含 accuracy / parse_rate / errors / token 用量）
- [x] `[auto]` MCQ runner 单测：mock LLM 返回特定 option → 断言准确率计算正确（29 单测：parse_mcq_answer 各种格式 / score_item 一致性 / aggregate_results 重算 / `_FakeChatClient` 端到端 3-of-4 acc=0.75 / 双模型 acc 独立 / write_report markdown+JSON 落地）

### M7.3 Langfuse Dataset 集成 ✅ 2026-05-20

> M7.3 完成报告：[`../04-handoff/2026-05-20-m7.3-complete.md`](../04-handoff/2026-05-20-m7.3-complete.md)

- [x] `[auto]` `eval/langfuse_dataset.py`：`push_golden_to_langfuse(golden_path, dataset_name="tgpp-golden-v1")` 一次性把 `v1.yaml` 全集 push 到 Langfuse Dataset；按 `GoldenItem.id` 幂等 upsert（SDK 文档保证 "Upserts if an item with id already exists"）；缺 key / SDK 异常 → 返回 0 + log；单条 item 失败不阻塞其他（2026-05-20 落 + 5 单测覆盖）
- [x] `[auto]` runner 跑时给每条 item 创建 trace + 上传 score：`run_eval(langfuse_run_label="...")` 触发；`make_eval_trace_id` 按 `(label, item.id)` seed 生成幂等 trace_id；`create_event` 写 question/answer 到 trace；`push_run_score` 上传 9 个 NUMERIC（`context_recall_section/spec` / `fact_coverage` / `must_say_not_found_passed` / `forbidden_violation` / `ragas_faithfulness/answer_relevance/context_recall/context_precision`）；缺 key 自动 disable（2026-05-20 落 + 16 单测覆盖含 mock SDK / 缺 trace_id / 单 metric 失败隔离 / runner 集成）
- [ ] `[human]` Langfuse Cloud Web UI 验证：M7.2 评估结束后人触发首次 `push_golden_to_langfuse` → Cloud Dataset 页面确认 175 题可见 → 启用 built-in evaluators（faithfulness / relevance）关联 Dataset → 跑一次 daily 子集（`langfuse_run_label="m7-smoke-..."`）确认 trace 出现 + 出分

### M7.4 成本与用量监控

- [ ] `[auto]` `backend/app/llm/pricing.py`：单价表（mimo-v2.5-pro / mimo-v2.5 / voyage-4-large / voyage-rerank-2.5 / tavily-search），免费额度区间用 `billed=false` 标记
- [ ] `[auto]` `backend/app/services/usage.py`：LLM / Embedding / Rerank / WebSearch 4 路 hook，从 LiteLLM 响应 `usage` 字段取 token；写 ApiUsage 按 `(user_id, day)` upsert；rerank 按 `query_tokens × n_docs + Σ doc_tokens` 计费（Voyage 口径）
- [ ] `[auto]` LiteLLM client 在 `chat_completion` / `embedding` / `rerank` 返回处调 usage hook（侵入最小，不改业务路径）
- [ ] `[auto]` `backend/app/services/alerts.py`：每日聚合 job（apscheduler 进程内 cron） + 阈值（`.env` 覆盖：日 $5 / $10 / 月 $50） → 仅 log warning（决策 Q2）
- [ ] `[auto]` unit 覆盖 4 路径 + alerts 阈值边界（mock 用量数据 → 断言 log warning 行为）；`/admin/stats` 集成测从 ApiUsage 真实数据查询

### M7.5 Batch C 技术债（retrieval 校准）

- [ ] `[auto]` C.2 R10/R11/R19 retrieval 校准：根据 daily eval 暴露的 `proc-005` 等问题数据 drive 调 `backend/app/retrieval/{dense,sparse,hybrid,rerank}.py` 参数（top_k / RRF k / rerank top_k）；变更前后用 daily 子集对照
- [ ] `[auto]` C.3 O2 rerank ablation：同一份 daily 子集在 `tgpp_chunks_voyage_d1024` 上跑 baseline（dense+BM25+RRF）vs 加 voyage rerank-2.5；spec R@10 / section R@10 / MRR 提升曲线归档 `eval-results/m7-rerank-ablation.md`
- [ ] `[auto]` C.4 `test_retrieve_node_p50_latency_under_800ms`：选项 A 调宽阈值到 1200ms 加注释 / 选项 B 剔除 outlier 后取 p50（agent 自决）

### M7.6 Daily / Weekly CI + 完成验收

- [ ] `[auto]` `.github/workflows/eval-daily.yml` cron 每日 02:00（UTC+8）跑 `make eval-daily`；阈值未达自动开 GitHub issue（mock 验证：手动塞失败结果触发 issue 创建）
- [ ] `[auto]` `.github/workflows/eval-weekly.yml` cron 每周一 03:00 跑全集（`make eval-weekly`）；上传 results.json + report.md 到 artifact
- [ ] `[auto]` Daily eval 连跑 2 次 ≥ D13 第一档阈值
- [ ] `[auto]` 最终回归：`make lint` + `pytest -m unit` + `pytest -m integration`（backend + ingestion）+ `pytest -m eval`（daily 子集）全绿
- [ ] `[human]` 交付 `docs/04-handoff/yyyy-mm-dd-m7-complete.md` 完成报告

### 非 M7 范围（保留行）

- [x] `[human]` M3 embedding POC 决胜**决策由人拍板**：✅ 1024 维（2026-05-16），结果与签字记录在 `eval-results/m3-embedding-poc.md`
- [ ] `[M5]` 前端管理后台展示 today / month 成本（widget test 覆盖渲染） — **挪到 M5**，M7 只保证 `/admin/stats` 数据真实

## 13. 风险与排雷

| 风险 | 触发 | 应对 |
|------|------|------|
| TeleQnA 部分答案过时或与 Rel-18/19 不符 | 数据集发布于 2023 | 转化阶段人工校验剔除；保留 `teleqna_origin_id` 便于追溯 |
| LLM 转化误把"选项排除题"做成开放题 | "以下哪个不属于"类 | 转化 prompt 检测此类题型，跳过 → 进 `_rejected.yaml` |
| TeleQnA 解释字段引用的 spec 与现行版本编号不同 | spec 重命名 / 拆分 | M3 校验时手工映射；维护 `teleqna_spec_alias.yaml` |
| 金标准集主观偏差 | 一人写一人评 | 标注规范文档化；M7 期请第二人 sanity check 10 题 |
| Ragas 评分本身不稳 | LLM 评估随机性 | 评估固定 temperature=0；M7 暂不多跑取均值（成本控制 Q1） |
| Langfuse Cloud 网络抖动 | 国内访问 | 写入 retry + 本地落盘 fallback；监控 ingest 失败率；缺 key 时 runner 仍可跑（M7.3 容忍） |
| 评测 LLM 与 Agent LLM 同源偏差 | 都用 mimo | 明确 Ragas judge 用 `glm-5.1`（已在 LiteLLM） |
| CI eval 超时 | daily 20 题但 Agent 慢 | daily 子集偏 hand_crafted 高信号题；并发 2-3 题；timeout 30min |
| 全集每周一次 + daily 20 题预算超支 | 模型涨价 / 题目复杂化 | M7.4 alerts 仅 log；预算超 ¥1000/月 → 触发 §5.10 上报，降配 daily 隔日跑 |

## 14. 完成后下一步

→ `07-cicd-and-deployment.md` 把 CI / 生产部署 / HTTPS 收尾。
