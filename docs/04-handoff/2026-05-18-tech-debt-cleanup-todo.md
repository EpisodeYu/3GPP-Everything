# 2026-05-18 · 技术债清理批次执行清单

> 基于 [`2026-05-17-m4.5-status-snapshot.md`](2026-05-17-m4.5-status-snapshot.md) 2026-05-18 清理批次决策产出。
> 本文件是后续 agent 的"按需消费"待办清单：每个 batch 对应一个或多个里程碑期间穿插完成；不要把所有 batch 一次性当一个大任务做。
>
> **关联决策**（2026-05-18 人审通过的 Q1-Q6）：
>
> | # | 决策 | 结论 |
> |---|---|---|
> | Q1 | D13 评测阈值 | **two-tier**：M7 nightly 用宽松版（faithfulness ≥ 0.75 / context recall ≥ 0.65 等）；M8 上线门槛用严格版（0.85 / 0.80） |
> | Q2 | R12 NodeInterrupt 迁移 | M4.8 期间统一迁移到 `langgraph.types.interrupt` |
> | Q3 | R8 + O3 self-RAG citation 核对 | M4.9 期间穿插实装 |
> | Q4 | R4 + O5 separator unit test | M4.8 期间穿插补 |
> | Q5 | R3 chunk_id root cause | 保留 dedupe 兜底 + 现有 unit test 标 done |
> | Q6 | R18 passlib 依赖清理 | M4.8 期间删依赖 |

---

## 一、执行节奏

| Batch | 触发里程碑 | 范围 | 估工时 | 阻塞关系 |
|---|---|---|---|---|
| **A** | M4.8 启动时 | R4/O5 + R12 + R18 + 文档同步 4 项小修 | ~4h | 与 M4.8 5 路由并行；任一可独立提交 |
| **B** | M4.9 启动时 | R8/O3 self-RAG citation 核对 | ~2h | 与 M4.9 5 路由并行 |
| **C** | M7 启动前 | D13 阈值收紧 PR 准备 + R10/R11/R19 retrieval 校准 + O2 rerank ablation | ~1d | 必须先 M7 nightly 跑通宽松版 |
| **D** | M8 启动前 | R6/R7/R13/R16 + D9/D10/D11 + O9/O10/O15 + checkpoint GC + 阈值收紧最终 PR | 整段 M8 周期 | M5 / M7 都通过后 |
| **E** | M5 期间 | R9 verdict 分级 + O12-O14 前端体验 | 与 M5 主交付一起 | M4 全绿后 |
| **F** | 长期 backlog | O4 / O7 / O8 / O11 / O16-O23 | 无固定节奏 | 无 |

---

## 二、Batch A — M4.8 期间穿插（4 小项）

> M4.8 主交付物：`api/v1/checkpoint.py` 5 路由（pause / resume / list / fork / rollback）。下述 4 项与主交付**完全独立**，可在 M4.8 任一段穿插完成；建议合到 M4.8 完成报告里一并提交。

### A.1 R4 + O5 · Table separator unit test 兜底

**目标**：防止 ingestion markdown_parser 的 table separator 兜底（~93% 覆盖率）回归。

**文件**：`ingestion/tests/unit/test_section_splitter.py`（或新增 `ingestion/tests/unit/test_markdown_parser_table.py`）

**实施步骤**：
1. 用 Grep 找 `ingestion/hf_loader/markdown_parser.py` 中处理 table separator 的兜底函数（关键字 `separator` / `---|---` / `table`）
2. 准备 3 个 fixture：
   - **正常**：完整 `| h1 | h2 |\n|---|---|\n| v1 | v2 |` → 解析为 markdown table
   - **缺 separator**：`| h1 | h2 |\n| v1 | v2 |`（POC 38.331 发现的 ~7% case）→ 兜底插入 separator 后能正常 render
   - **多 row 缺 separator**：3+ 行连续缺 separator → 兜底逻辑稳定
3. 断言：每条 fixture 走完 parser 后输出的 markdown 字符串包含合法 `|---|` 分隔行
4. `uv run pytest ingestion/tests/unit/test_section_splitter.py -v` 全绿

**完成标记**：在 `2026-05-17-m4.5-status-snapshot.md` R4 / O5 行加 `✅ DONE (M4.8 batch A.1)`。

---

### A.2 R12 · NodeInterrupt 统一迁移到 `langgraph.types.interrupt`

**目标**：消除 9 个节点的 `DeprecationWarning`，避免 LangGraph v2 release 时 hard fail。

**当前状态**：所有 9 个节点 + `checkpoint.py` + `api/v1/chat.py` + `test_checkpoint_ops.py` 都用 `from langgraph.errors import NodeInterrupt`，行内 `raise NodeInterrupt("cancelled by user")` / `raise NodeInterrupt("paused by user")`。

**文件清单**（12 处：9 节点 + 2 包装层 + 1 测试）：
- `backend/app/agent/nodes/classify.py`
- `backend/app/agent/nodes/rewrite.py`
- `backend/app/agent/nodes/hyde.py`
- `backend/app/agent/nodes/multi_query.py`
- `backend/app/agent/nodes/retrieve.py`
- `backend/app/agent/nodes/rerank.py`
- `backend/app/agent/nodes/generate.py`
- `backend/app/agent/nodes/self_rag.py`
- `backend/app/agent/nodes/tool_dispatch.py`
- `backend/app/agent/checkpoint.py`
- `backend/app/api/v1/chat.py`
- `backend/tests/integration/agent/test_checkpoint_ops.py`

**实施步骤**：
1. 先核实 LangGraph 当前版本（`uv run python -c "import langgraph; print(langgraph.__version__)"`）下 `langgraph.types.interrupt` 的正确 API（参考 [LangGraph types docs](https://langchain-ai.github.io/langgraph/reference/types/)）。`interrupt` 是函数 `interrupt(value: Any) -> Any`（在 graph 中 raise 出去），不是 class。
2. 用 ripgrep 列全部 `from langgraph.errors import NodeInterrupt` 与 `raise NodeInterrupt(`：
   ```bash
   rg "from langgraph.errors import NodeInterrupt" backend/
   rg "raise NodeInterrupt\(" backend/
   ```
3. 对每个节点的 `raise NodeInterrupt("cancelled by user")` / `raise NodeInterrupt("paused by user")`：
   - 替换 `import` 为 `from langgraph.types import interrupt`（如果文件里只用 NodeInterrupt 没用别的）
   - 替换 `raise NodeInterrupt("...")` 为 `interrupt({"reason": "cancelled by user"})` / `interrupt({"reason": "paused by user"})`
   - 注意 `interrupt()` 的语义是把控制权交回，与 `raise NodeInterrupt` 行为等价但 v2 持续支持
4. 更新 `test_checkpoint_ops.py` 测试断言（如有断言 `NodeInterrupt` 类型，改为检查 `__interrupt__` 注入）
5. 全套回归：`make lint` + `pytest -m unit` + `pytest -m integration backend/tests/integration/agent/test_checkpoint_ops.py` 全绿
6. 确认没有新的 DeprecationWarning（`pytest -W error::DeprecationWarning -m unit` 局部跑一下）

**完成标记**：
- snapshot 文档 R12 行加 `✅ DONE (M4.8 batch A.2)`
- `docs/03-development/03-agent.md §M4.5 自主决策记录`(2) 去掉"NodeInterrupt 仍 work（带 DeprecationWarning），暂保留"的备注

**风险**：
- 如 `langgraph.types.interrupt` 当前 langgraph 版本签名与文档不符 → 停下问，**触发 CLAUDE.md §5.7（依赖大改动）**
- 如发现 `__interrupt__` 注入逻辑导致 graph 状态机行为偏离原 NodeInterrupt → 回滚，归 M5 前再做

---

### A.3 R18 · passlib 依赖清理

**目标**：从 `backend/pyproject.toml` 移除未使用的 `passlib[bcrypt]`。

**当前状态**：M4.6 已改用 bcrypt 直接调用（详见 `2026-05-18-m4.6-completion.md §3` 自主决策 #1）；`pyproject.toml:29` 仍列 `"passlib[bcrypt]>=1.7"`；`uv.lock` 仍记录。

**实施步骤**：
1. 全局确认 passlib 在 backend 代码中未被引用：
   ```bash
   rg "import passlib|from passlib" backend/app backend/tests
   ```
   应为 0 命中（如果有，先停下问）
2. 改 `backend/pyproject.toml`：删除 `"passlib[bcrypt]>=1.7"` 一行
3. 重生成 lock：`cd backend && uv lock`
4. 全套回归：`make lint` + `pytest -m unit` + `pytest -m integration backend/tests/integration/api/test_auth.py`（M4.6 鉴权集成测）必须全绿
5. 在 commit 里说明"M4.6 完成报告自主决策 #1 跟进：删除残留 passlib 依赖"

**完成标记**：snapshot 文档 R18 行加 `✅ DONE (M4.8 batch A.3)`。

---

### A.4 文档同步（D13 two-tier 阈值）

**目标**：把 D13 two-tier 决策落到 `docs/03-development/00-overview.md`。

**当前状态**：
- `docs/03-development/00-overview.md §3 M7` 行写：`关键决策点 = 评测阈值是否符合验收（faithfulness ≥ 0.85、context recall ≥ 0.80）` — 这是严格版（M8 门槛）
- `docs/03-development/06-evaluation-and-observability.md §6.2`（line 320-321）写：`avg_recall >= 0.6, avg_faith >= 0.75` — 这是宽松 subset 版
- `docs/03-development/06-evaluation-and-observability.md §6.3`（line 334-335）写：`context_recall_section >= 0.80, ragas_faithfulness >= 0.85` — 严格版
- `docs/04-handoff/2026-05-18-m4.7-completion.md §4.4` 提了一组新阈值：`faithfulness ≥ 0.75 / context recall ≥ 0.65 / answer relevancy ≥ 0.70 / answer correctness ≥ 0.55 / latency-p50 ≤ 6s / cost-p50 ≤ ¥0.30`

**实施步骤**：
1. 改 `docs/03-development/00-overview.md §3` M7 行的"关键决策点"列：
   ```
   评测阈值分两档：M7 nightly 用宽松版（faithfulness ≥ 0.75 / context recall ≥ 0.65 等）；
   M8 上线门槛用严格版（faithfulness ≥ 0.85 / context recall ≥ 0.80）。
   详见 2026-05-18-tech-debt-cleanup-todo.md Q1 决策与 06-evaluation-and-observability.md §6-§7
   ```
2. 改 `docs/03-development/06-evaluation-and-observability.md §6`：明确两档阈值表（如已存在则只加一行注释指向本 todo 文件）
3. 不动 `docs/03-development/06-evaluation-and-observability.md §6.3` 的代码（严格版 assert 保留，M8 时跑）

**完成标记**：snapshot 文档 D13 行已写明 ✅ 不需再改。

---

## 三、Batch B — M4.9 期间穿插（1 项）

### B.1 R8 + O3 · self-RAG citation 真实性核对 ✅ DONE (2026-05-18)

**目标**：在 `self_rag_node` 中加 set-intersection 校验，拦截 LLM 输出的 `[spec_id §section_path]` 不存在于 retrieved chunks 的 hallucination。

**文件**：`backend/app/agent/nodes/self_rag.py`（约 +20 行）+ `backend/app/agent/nodes/generate.py`（如 citation 抽取在那里，复用现有正则即可）

**当前状态**：
- M4.2 自主决策记录提到 `re.compile(r"\[(spec)\s*§(sect)\]")` 对 `chunk.section_path` 做**前缀匹配**（已实装在 generate_node）
- self_rag_node 只判 verdict + missing_aspects，**不**校验 citations 是否落在 reranked chunks 集合内

**实施步骤**：

1. 在 `self_rag_node` 加一段在 LLM 校验后、return 前的校验：
   ```python
   # citation 真实性核对（R8 + O3）：用 reranked 集合反查
   #   - 取 state.citations 里的 (spec_id, section_path)
   #   - 与 state.reranked 集合做 set-intersection（沿用 generate_node 的前缀匹配语义）
   #   - 命中率 < 0.5 → 强制 verdict=retry（如 allow_retry）或降级 confidence
   ```
   语义参考（参与设计的 agent 自主决定具体实现）：
   - 全部 citation 都在 reranked 内 → 不动
   - 命中率 [0.5, 1.0) → 保留 verdict 但 `confidence *= 命中率`
   - 命中率 < 0.5 且 `allow_retry=True` → 强制 verdict=retry，把"未命中的 citation 涉及的 spec"作为 missing_aspect 加到 rewritten_queries
   - 命中率 < 0.5 且 `allow_retry=False` → 强制 verdict=accept 但 `confidence = 0.0`（M4.2 simple fast path 不死循环原则保留）
2. 在 `backend/tests/unit/agent/test_self_rag.py`（如不存在则新建）加 3 个 unit case：
   - 全 grounded citations：confidence 不动
   - 部分 hallucinate（命中率 50-99%）：confidence 下降但 verdict 保留
   - 大部分 hallucinate（命中率 < 50%）：simple path → accept + confidence=0；complex path → retry
3. 跑 `pytest -m unit backend/tests/unit/agent/` 全绿
4. 跑 `pytest -m integration backend/tests/integration/agent/test_simple_qa.py test_complex_qa.py` — 真实环境的 retrieval 召回质量问题（R19 提到的 proc-005）此时**可能**因 citation 核对触发 retry 而走向正确路径，是 nice-to-have；不强求修复 R19。

**完成标记**：snapshot 文档 R8 / O3 行加 `✅ DONE (M4.9 batch B.1)`。

**风险**：
- 如核对逻辑过严导致 simple path confidence 普遍降为 0 → 用户体验下降；保留命中率 0.5 阈值作为旋钮
- 如 complex path retry 触发率上升导致成本增加 → M7 nightly eval 监控 cost-p50

---

## 四、Batch C — M7 启动前（评测阈值与 retrieval 校准）

### C.1 D13 第一档阈值落地

**实施**：在 `tests/eval/test_golden_v1.py`（M7 落地时新增）里跑宽松阈值断言：
```python
assert mean(r.context_recall_section for r in results) >= 0.65
assert mean(r.ragas_faithfulness for r in results) >= 0.75
assert mean(r.ragas_answer_relevance for r in results) >= 0.70
assert mean(r.ragas_answer_correctness for r in results) >= 0.55
assert percentile(latencies, 50) <= 6.0
assert cost_p50_cny <= 0.30
```

详见 `docs/03-development/06-evaluation-and-observability.md §6.2`。

### C.2 R10 / R11 / R19 · retrieval 召回校准 ✅ DONE (M7.5, 2026-05-22)

**实际做的**：

1. 启动盘点时发现一个生产 hotfix：`LiteLLMClient.embed` 给 LiteLLM proxy 传的是 OpenAI 的 `dimensions` 字段，但 LiteLLM 透传 voyage 时只认 voyage 自家的 `output_dimension` → 返回默认 2048 维 → 与 d1024 collection 维度不匹配 → 生产 **dense 一直返回空，BM25 sparse-only 在跑**。修法 1 行 + 2 单测；container reload 实证 dense 跳中。
2. 新增 `backend/scripts/dev/retrieval_ablation.py`（dev 工具 + 19 单测）；hand_crafted 56 题 × 7 config 扫描。
3. 默认参数从 `dense30/sparse30/rrf60/top50/rerank5` 改为 `dense50/sparse50/rrf60/top80/rerank5`：实测 section_recall@5 75→80%、spec_recall@5 85→92.5%、MRR 0.706→0.711，p50 持平 605ms。

详见 [`2026-05-22-m7.5-complete.md`](2026-05-22-m7.5-complete.md) + [`../../eval-results/m7-rerank-ablation.md`](../../eval-results/m7-rerank-ablation.md)。

### C.3 O2 · Rerank ablation ✅ DONE (M7.5, 2026-05-22)

报告归档 `eval-results/m7-rerank-ablation.md`。**关键发现**：

- rerank 收益明确：no-rerank vs rerank5 在 section@5 +2.5pp、spec@5 +5pp、MRR +0.07
- RRF k ∈ {30, 60, 100} 在 rerank 下游被洗掉，无差异
- rerank_top_k=10 vs 5 仅在 section@10 +2.5pp，section@5 持平 → 不入默认（避免下游 generate prompt context 翻倍）
- wider candidate pool（dense/sparse 50, final_top_n 80）+ rerank5 给出 section@5 80% / spec@5 92.5% 综合最优

### C.4 `test_retrieve_node_p50_latency_under_800ms` 处理 ✅ DONE (M7.5, 2026-05-22)

选 **B 改进**：

- 加 2 题 warmup 吃 BM25 / voyage / qdrant 连接池 cold-path（warmup 不计入 timings）
- 5 题取中位数 P50（outlier-resistant）
- 硬阈值 800 → 1500ms 给 voyage 外网 RTT + 物理机噪声宽余量
- 设计目标 800ms 不动（docs/03-development/03-agent.md §M4.2）；M8 上线如真稳定到 < 800ms 可再收紧

详见 [`2026-05-22-m7.5-complete.md §3.4`](2026-05-22-m7.5-complete.md)。

---

## 五、Batch D — M8 上线前

### D.1 D13 第二档阈值收紧 PR

M7 → M8 之间一个独立 PR：把 `tests/eval/test_golden_v1.py` 的断言从 C.1 宽松版改为：
```python
assert mean(r.context_recall_section for r in results) >= 0.80
assert mean(r.ragas_faithfulness for r in results) >= 0.85
# 其它维度阈值收紧 PR 中具体定
```
连跑 2 次绿才能合并进 M8 上线。

### D.2 R14 + D12 · paused/失败 run checkpoint GC

实装后台清理任务：每天扫一次 `sessions.status='paused'` 且 `updated_at < now - retention_days` 的会话；删 PG `messages` + `langgraph_checkpoints` 对应行；retention_days 默认 30 天，从 env 读。

### D.3 R15 · Nginx SSE 配置验证

`07-cicd-and-deployment.md §11` 的 Nginx 配置（`proxy_buffering off` + `proxy_read_timeout 75s`）在生产 Compose 实跑一次，curl 30s+ 长答案不断流。

### D.4 R6 · Voyage → Jina rerank fallback CI

- 在 `backend/app/retrieval/rerank.py` 加 fallback 路径（已有 / 待加）
- 在 `tests/integration/retrieval/test_rerank_fallback.py` 用 mock voyage timeout → 触发 Jina 调用
- CI 跑通

### D.5 R7 · Vision 缓存备份脚本

`scripts/ops/dump_vision_cache.py`：从 Redis `tgpp:cache:vision:*` dump 到磁盘 jsonl，附 README"清空 Redis 前必须先跑此脚本"。

### D.6 R13 · LiteLLM proxy HA

调研：本机另起一个 LiteLLM 实例 + haproxy / nginx active-passive；或接受单点+ 监控告警。M8 上线前由人审一次决定。

### D.7 R16 · 2 核 worker=3 调优

线上 1 周流量基线后看 worker 数 / async pool size / PG conn pool 调优。

### D.8 D9 · Docling 兜底 + `POST /admin/upload-doc`

按 `docs/03-development/04-backend-api.md §2 / §9` 实装；接 ingestion 现有 Docling 兜底链路。

### D.9 D10 · Redis Streams + 独立 worker

按 `docs/03-development/04-backend-api.md §9.2` 实装：`worker.py` 进程 + consumer group + `XADD/XACK`；替换 M4.10 的 `asyncio.create_task` 简化版。

### D.10 D11 · Langfuse 数据保留策略

设置 trace retention（按 plan 文档定，cloud 默认 30 天 / self-host 自定）+ 月度成本告警。

### D.11 R20 · M4.10 端到端人审已暴露的 SSE schema 偏差

M4.10 阶段如发现 `astream_events("v2")` 字段与 fake fixture 偏差 → 修对应 mapping；M8 上线前必须修完。

### D.12 O9 · BM25 持久化"成品"

如 backend 冷启动延迟 > 30s → 实装 pickled in-memory index；否则保留 jsonl 加载。

### D.13 O10 · PG 连接池 / checkpointer 写入频率调优

多用户压测后量化，与 R16 一起做。

### D.14 O15 · CLI 进度条与 ETA

`ingestion` / `admin` 操作加 `tqdm` 进度条。

---

## 六、Batch E — M5 前端期间

### E.1 R9 + O13 · verdict 分级展示

Flutter 阅读器 chip 染色：
- `verdict=accept` + `confidence >= 0.8` → 绿色
- `verdict=accept` + `confidence ∈ [0.5, 0.8)` → 黄色
- `verdict=accept` + `confidence < 0.5` → 灰色（自动 retry 后强制 accept 的低质答案）
- `verdict=retry` 跑完仍未 accept → 红色"未在已索引文档中找到"

### E.2 O12 · Citation `char_offset` 精确化

前端阅读器跳转到 chunk 内段落精确位置（chunks_meta 已有 `char_offset_start` / `char_offset_end`）。

### E.3 O14 · 阅读器图片预加载/缩略图

Vision 描述加上原图缩略图，hover 显示大图。

---

## 七、Batch F — 长期 backlog

无固定执行时点，由后续 agent 评估收益后挑：

- **O4** · Glossary 21.905 补齐（数据源未发布前无解；HF dataset ready 后由 `_ALWAYS_INCLUDE_SPEC_IDS = {"21.905"}` 自动 pick up）
- **O7** · Chunker 多进程并行（服务器扩容后）
- **O8** · Vision concurrent 提至 16-32（mimo RPM 扩限后）
- **O11** · 历史消息分级存储（M4.7 内存压缩已足够；长期归档对象存储）
- **O16** · mypy / pyright 严格模式
- **O17** · 单元测试覆盖率 ≥ 85%
- **O18** · API Usage 细颗粒度统计（模型/工具/用户分桶）
- **O19** · Handoff 文档自动化
- **O20** · Prompt md ↔ 代码常量同步 linter
- **O21** · Generate prompt 3GPP 定制（当前通用模板）
- **O22** · HyDE prompt 3GPP-aware 改造
- **O23** · Small2Big parent section 大小动态化

---

## 八、Agent 实施约定

1. **不要一次性把 Batch A-F 全做完**。M4.8 启动时只做 Batch A；M4.9 启动时只做 Batch B；以此类推。
2. **每完成一个 batch item** → 在本文件对应行加 `✅ DONE (实施日期 / commit hash)`，并同步更新 `2026-05-17-m4.5-status-snapshot.md` 对应 R/D/O 行。
3. **任一 batch item 触达 CLAUDE.md §5 上报条件**（如 R12 NodeInterrupt 迁移过程中发现 LangGraph API 大改）→ 停下问人，不要自作主张。
4. **每个 batch item 必须满足 vibe coding §4 自动化测试硬要求**：业务代码改动必有 unit / integration 测试覆盖。
5. **commit message** 用 Conventional Commits，建议 batch 维度：`refactor(M4.8): batch A.2 — NodeInterrupt 迁移到 langgraph.types.interrupt`。

---

**记录人**：Agent（vibe coding 模式，2026-05-18）
**人审**：2026-05-18，Q1-Q6 由人在 chat 中明确选择
**关联文档**：`2026-05-17-m4.5-status-snapshot.md`（2026-05-18 清理批次更新） / `2026-05-18-m4.6-completion.md` / `2026-05-18-m4.7-completion.md`
