# 2026-06-02 · Map-Reduce 检索（A 范式）实现文档

> 起源：[`2026-06-02` multi-agent 范式调查] 结论——在本项目吞吐/延迟/grounding 约束下，
> 唯一值得部署的"多 agent"协作结构是**子问题分解 + 每子问题独立 retrieve/rerank +
> 配额合成**（受限 orchestrator-worker / map-reduce）。它本质是把现有 complex 路径
> 从"伪 map（共享候选池 + 只用 primary query 重排）"升级成"真 map-reduce"，
> **不引入任何额外 LLM 扇出**，因此不撞本机 LiteLLM proxy 吞吐瓶颈。
>
> 本文档 = 可直接落地的实现规格。**本次只写文档，不改代码。**
>
> 锚：
> - 现状根因：[`03-development/03-agent.md §4.4/§4.5/§4.6`](../03-development/03-agent.md)
> - 实测缺口数据：[`2026-05-30-ragas-4metric-uplift-results.md §0/§3.4`](2026-05-30-ragas-4metric-uplift-results.md)
> - 涉及文件：`backend/app/agent/nodes/{retrieve,rerank}.py`、`state.py`、
>   `backend/app/core/config.py`、`backend/app/retrieval/{hybrid,rerank}.py`

## 0. TL;DR

| 项 | 结论 |
|---|---|
| 解决什么 | multi_section 类 ctx_recall 卡 **0.61**（实测天花板≈0.5）：多 section 题单题只召回 1/2 section |
| 根因 | (1) multi_query 的 N 条子查询被 `rrf_merge` 压成**一个** top-80 池；(2) `rerank_node` **只用 `rewritten_queries[0]`（primary）** 重排后截 top-8 → 强势 facet 把其它 facet 挤出 top-8 |
| 方案 | 每个子查询**独立** retrieve + **用各自子查询** rerank，再**轮转配额合并**保证每个 facet 至少进 top-1 |
| 关键性质 | map 阶段 = 检索 + rerank（**0 次额外 LLM**）；reduce = 纯函数合并；generate 仍 **1 次** LLM → **不增加 mimo proxy 压力** |
| 触发范围 | 仅 `complexity=="complex" 且 query_class!="definition"`（definition 保留已调好的 single-pool title-boost 路径）；flag 默认关 |
| 图拓扑 | **不变**（retrieve→rerank→generate，retry→retrieve）；只改两个节点内部行为，按 flag 分支 |
| 引用契约 | **不破坏**：`state.reranked` 仍是最终列表，`[N]` 仍指 `reranked[N-1]` |
| 成本 | mimo LLM 调用 **0 增量**；Voyage rerank 调用 ×N（免费额度内）；generate context 由 8→budget（默认 12）chunk，略增输入 token |
| 验收门槛 | multi_section 8 题 ctx_recall ↑（目标 0.61→≥0.72），**且 faithfulness 不掉**（防止多塞 chunk 诱发信息倾倒） |
| 工作量 | M（2 个节点改造 + 1 个纯函数 + settings + 测试 + eval 子集验证） |

## 1. 现状根因（带代码证据）

### 1.1 multi_query 产出 N 条子查询，但被压成一个池

`nodes/multi_query.py`：输出 `rewritten_queries = [primary, sub1, …, sub5]`（最多 1+5）。

`nodes/retrieve.py:68-91`：对**所有** query 逐条 dense+sparse，然后**一次** `rrf_merge`
跨全部结果融合，截到 `RETRIEVAL_FINAL_TOP_K`（=80）：

```python
for q in queries:
    d  = await deps.dense.retrieve(q, top_k=50)
    sp = await deps.sparse.retrieve(q, top_k=50)
    ...
fused = rrf_merge(*dense_lists, *sparse_lists, k=60, top_n=80)   # ← 单池
```

### 1.2 rerank 只用 primary query

`nodes/rerank.py:71`：

```python
query = state.rewritten_queries[0] if state.rewritten_queries else state.user_input
...
reranked = await deps.reranker.rerank(query, cands, top_k=pool_k)   # ← 只用 primary
out = ranked[: RERANK_TOP_K]   # =8
```

**后果**：一个对 sub-query 3 高度相关、但对 primary 一般的 chunk，在 rerank 阶段被压下去。
多 section 题需要 A+B+C 三段证据时，若 A 段 chunk 多且强，top-8 可能被 A 段占满，
B/C 段丢失 → ctx_recall 天花板≈0.5。

### 1.3 实测确认

`2026-05-30-ragas-4metric-uplift-results.md §3.4` 原文：

> multi 类需要 2-3 个 section 联合，**单题通常只召回 1/2 section，facts 覆盖率天花板≈0.5**。
> 这是 **multi-query / HyDE 设计能再投资的方向**。

按 category（v11 final）：multi_section ctx_recall = **0.612**，formula = 0.521。
（formula 的低分是 ingestion LaTeX 抽空，**非本方案能修**，见 §7。）

## 2. 设计

### 2.1 核心算法

**Map（per sub-query q_i，i=1..N）：**
1. retrieve：`dense(q_i, 50)` + `sparse(q_i, 50)` → `rrf_merge` → 取 `PER_QUERY_POOL`（=30）条 = `pool_i`
2. rerank：`voyage.rerank(q_i, pool_i, top_k=PER_QUERY_TOPM)`（=4）→ `ranked_i`

**Reduce（轮转配额合并）：**
- 轮转交错取：`ranked_1[0], ranked_2[0], …, ranked_N[0], ranked_1[1], …`
- 按 `chunk_id` 去重
- 直到收满 `MAPREDUCE_BUDGET`（=12）条 → `state.reranked`

轮转保证**每个 facet 的 top-1 都先于任意 facet 的 top-2 入选** → 没有 facet 被挤掉。
N=6、budget=12 时每 facet ~2 条；即使 budget=8 每 facet 仍 ~1.3 条，严格优于现状
（现状可能 1 个 facet 独占 8 条）。

### 2.2 为什么不增加 LLM 扇出（关键卖点）

map 阶段只有**检索 + Voyage rerank**，没有 mimo 调用；reduce 是纯函数；generate 仍
**1 次**。所以本方案对本机 LiteLLM proxy（mimo）的压力 **0 增量**——这正是它能在
"并发会撞爆 proxy"的约束下落地、而全量 orchestrator-worker / debate 不能的原因。
（如未来要做"每子问题独立 generate 子答案再 synthesize"的重版本，会有 N+1 次 mimo
调用——撞吞吐 + 爆延迟，**本方案不走这条**，见 §8 备选。）

### 2.3 图拓扑不变（最小侵入）

不新增节点、不改边。`build_graph` 的 `retrieve → rerank → generate`、self_rag
`retry → retrieve` **全部保持**。只在 `retrieve_node` / `rerank_node` 内部按
flag + 触发条件分支到 map-reduce 行为。好处：

- simple 路径（1 条 query）天然退化为现行为，零额外成本；
- self_rag retry：`missing_aspects` 仍 append 到 `rewritten_queries` → 下一轮
  retrieve 看到更多 query → map-reduce 自动把新 facet 纳入，**retry 语义不动**；
- definition 路径：见 §2.4 排除，保留现有 title-boost 路径不受影响；
- 既有单/集成测的节点序列断言不被破坏。

### 2.4 触发条件（精确）

`retrieve_node` / `rerank_node` 进入 map-reduce 分支当且仅当：

```
deps.settings.RETRIEVAL_MAPREDUCE_ENABLED is True
  AND state.complexity == "complex"
  AND state.query_class != "definition"     # definition 保留 single-pool title-boost
  AND len(effective_queries) > 1            # 单 query 无意义，退现行为
```

其中 `effective_queries` = 去掉 `hyde_doc` 后的 `rewritten_queries`（见 §3.3 hyde 处理）。
任一条件不满足 → 走现有逻辑（**完全向后兼容**）。

## 3. 代码改动清单

### 3.1 `backend/app/core/config.py`（新增 4 个 settings）

紧跟现有 `RERANK_TOP_K`（line 154）后：

```python
# Map-reduce 检索（A 范式，仅 complex 非 definition 路径；默认关，eval 验证后再开）
# 口径见 docs/04-handoff/2026-06-02-mapreduce-retrieval-plan.md
RETRIEVAL_MAPREDUCE_ENABLED: bool = False
RETRIEVAL_MAPREDUCE_PER_QUERY_POOL: int = 30   # 每子查询喂给 rerank 的候选数（控 Voyage doc 量）
RETRIEVAL_MAPREDUCE_PER_QUERY_TOPM: int = 4    # 每子查询 rerank 后保留 top-m
RETRIEVAL_MAPREDUCE_BUDGET: int = 12           # 轮转合并后总 chunk 数（喂 generate）
```

> 4 个参数都属 CLAUDE.md §4.3 "已在文档划定区间内"的可调参数；区间见 §6。
> `.env.example` 可不暴露（有合理默认）；若暴露需同步
> `docs/03-development/01-infrastructure.md §2.4`（CLAUDE.md §8）。

### 3.2 `backend/app/agent/state.py`（新增 1 个字段）

`AgentState` 检索段（line 108-110 附近）加：

```python
# map-reduce：每子查询独立 retrieve 后的候选列表（rerank_node 消费做 per-query 重排）。
# 仅 map-reduce 分支写；现行 single-pool 路径恒为空。可被 PostgresSaver 序列化。
candidates_by_query: list[list[RetrievedChunk]] = Field(default_factory=list)
```

`RetrievedChunk` 已是 pydantic BaseModel（checkpoint 安全），嵌套 list 无新序列化风险。

### 3.3 `backend/app/agent/nodes/retrieve.py`

加一个 `_mapreduce_retrieve(state, deps)` 分支，在 `retrieve_node` 顶部按 §2.4 触发条件分流：

- 对每个 `q_i`（不含 hyde_doc）：`dense(q_i,50)+sparse(q_i,50)` → `rrf_merge(..., top_n=PER_QUERY_POOL)` → `pool_i`
- `state.candidates_by_query = [pool_i...]`
- 同时仍设 `state.candidates`：用全部 `pool_i` 再 `rrf_merge` 一次取 `RETRIEVAL_FINAL_TOP_K`（给 SSE `chunks_hit` + cache + fallback 用，**语义不变**）
- hyde_doc 处理：hyde 文档**只进 flat candidates 池**（作为额外召回信号），**不**单独成一个 facet（它不是"角度"，是理想答案；单独 rerank 无意义）
- `chunks_hit` emit：照旧用 flat candidates top-10（前端契约不变）
- 缓存 key 扩展：mapreduce 分支 cache payload 里加 `{"mode":"mapreduce"}` 区分，避免与 single-pool 结果串味

### 3.4 `backend/app/agent/nodes/rerank.py`

加 `_mapreduce_rerank(state, deps)` 分支：

- 触发：`state.candidates_by_query` 非空（即 retrieve 走了 map-reduce）
- 对每个 `(q_i, pool_i)`：`deps.reranker.rerank(q_i, pool_i, top_k=PER_QUERY_TOPM)` → `ranked_i`
  - 并发：`asyncio.gather` + `asyncio.Semaphore(3)` 限并发（Voyage rerank 经 LiteLLM proxy，限 3 路避免与其它请求争带宽；rerank 是轻量短调用，3 路足够压低延迟）
  - 单个 q_i rerank 失败 → 该 facet 退回 `pool_i` 的 fused_score top-m，不阻塞其它 facet
- 合并：`_round_robin_merge(ranked_lists, budget=MAPREDUCE_BUDGET)`（新纯函数，见 §3.5）→ `state.reranked`
- `chunks_rerank` emit：合并后**一次**，照旧（前端契约不变）
- `query_class=="definition"` 不会进这里（§2.4 已在 retrieve 层排除 → `candidates_by_query` 为空 → 走现有 single-pool + title-boost）

### 3.5 `backend/app/retrieval/hybrid.py`（新增 1 个纯函数）

与 `rrf_merge` 并列，便于单测：

```python
def round_robin_merge(
    ranked_lists: Sequence[Sequence[RetrievedChunk]], *, budget: int
) -> list[RetrievedChunk]:
    """轮转交错合并多个已排序列表，按 chunk_id 去重，截到 budget。

    保证每个非空 list 的 top-1 都先于任意 list 的 top-2 入选（facet 公平）。
    """
```

实现：`itertools.zip_longest` 横向轮转 + `seen` 去重 + budget 截断。

## 4. 边界 / 兼容性

| 场景 | 行为 |
|---|---|
| flag=False | 两节点走现有逻辑，`candidates_by_query` 恒空，**字节级等价现状** |
| simple 路径 | 1 条 query，§2.4 `len>1` 不满足 → 现行为 |
| definition | §2.4 排除 → single-pool + title-boost 不变（保住已验证的 section_recall 1.0）|
| self_rag retry | `missing_aspects` append 到 `rewritten_queries` → 下轮 retrieve 重跑 map-reduce，新 facet 自动纳入 |
| 引用契约 `[N]` | `state.reranked` 仍是最终列表，generate `parse_citations` → `reranked[N-1]` 不变 |
| SSE `chunks_hit/rerank` | 仍各 emit 一次，payload 结构不变 → 前端零改动 |
| 某 facet 全 rerank 失败 | 该 facet 退回 fused top-m，其它 facet 不受影响 |
| cache 命中 | mapreduce 与 single-pool 用不同 cache key（§3.3），不串味 |
| 老 checkpoint 反序列化 | 新增字段有默认值，旧 checkpoint 加载补默认空 list |

## 5. 成本 / 延迟分析

| 维度 | 现状（complex, 6 query） | map-reduce（6 facet） | 说明 |
|---|---|---|---|
| mimo LLM 调用 | classify+rewrite+hyde+multi_query+generate+self_rag | **完全相同** | map 阶段 0 LLM ← 核心 |
| Voyage rerank 调用 | 1 次（80 doc） | 6 次（各 30 doc，≈180 doc）| 免费额度 200M token 内；并发 3 路 |
| generate 输入 chunk | 8 | 12（budget）| +~50% generate 输入 token，bounded |
| 额外延迟 | — | rerank 6 次并发(sem=3) ≈ +1.5-3s | complex P95 预算 60s，富余充足 |

结论：**对吞吐瓶颈（mimo proxy）0 增量**；延迟在预算内；Voyage/generate 成本可控。

## 6. 参数区间（CLAUDE.md §4.3 自主可调）

| 参数 | 默认 | 区间 | 调参影响 |
|---|---:|---|---|
| `PER_QUERY_POOL` | 30 | 20-50 | ↑召回↑Voyage doc 量↑延迟 |
| `PER_QUERY_TOPM` | 4 | 2-6 | ↑每 facet 候选↑合并多样性 |
| `BUDGET` | 12 | 8-16 | ↑覆盖↑generate 成本↑信息倾倒风险（faithfulness↓）|
| 并发 semaphore | 3 | 2-5 | ↑降延迟↑proxy 争用 |

> ⚠️ `BUDGET` 是双刃：多塞 chunk 提 recall 但可能诱发 generate 信息倾倒，
> 重蹈 `2026-05-30` 报告里 rule6 v3 过冲（faithfulness 0.8→0.19）。**必须**配合
> §7 faithfulness 不降门槛验证；若降，先降 BUDGET 再考虑收紧 generate prompt。

## 7. 验收 / eval 验证门槛

### 7.1 自动化测试（CLAUDE.md §4.1 硬门槛）

**单元（`backend/tests/unit/`）：**
- `round_robin_merge`：轮转顺序 / chunk_id 去重 / budget 截断 / 空 list 跳过 / 单 list 退化
- `retrieve_node` mapreduce：3 query → `candidates_by_query` 长度 3 + flat `candidates` 仍非空；hyde_doc 只进 flat 不成 facet
- `rerank_node` mapreduce：mock reranker 断言**调用次数 == facet 数**；合并结果含 ≥2 个不同 facet 的 top-1；某 facet rerank 抛错时其余 facet 仍在
- 触发门：flag=False / simple / definition 三种场景**不**进 mapreduce（断言 `candidates_by_query` 空、rerank 调用 1 次）
- 回归：flag=False 时 retrieve/rerank 输出与改动前一致

**集成（`backend/tests/integration/agent/`）：**
- 构造多 facet complex query，端到端断言 `reranked` 含 ≥2 个不同 `spec_id`/section 的证据

### 7.2 eval 子集（决定是否上线的关键门）

用现有工具跑 **multi_section 8 题** before/after 对比（CLI 支持 `--include-category`，见 `eval/cli.py:89`）：

```bash
cd eval
# before（flag off）→ after（flag on）各跑一次，比 ctx_recall / faithfulness
uv run python -m eval.cli <run-subcommand> --include-category multi_section ...
# ragas 打分见 eval/ragas_eval.py；retrieval-only section_recall 见 runner_retrieval.py
```

> ragas judge 单 run 方差大（`2026-05-30` 报告 §5）：对照实验**各跑 ≥2 次取 max/mean 并报**，
> 别被单次 noise 误导。

**通过门槛（全绿才建议开 flag 上线）：**

| 指标 | 现状(v11) | 门槛 | 性质 |
|---|---:|---|---|
| multi_section ctx_recall | 0.612 | **≥ 0.72** | 主目标 |
| multi_section faithfulness | 0.851 | **不降（≥ 0.82）** | 硬护栏（防信息倾倒）|
| multi_section section_recall@10 | — | 升或持平 | retrieval-only 佐证 |
| definition/procedure 各项 | 见 v11 | **不回退**（应 untouched）| 回归保护 |

### 7.3 大功能回归（CLAUDE.md §4.2）

`make lint` + `make test`（unit+integration）全绿 + `ReadLints` 无新增。

## 8. 备选与权衡（why this shape）

| 备选 | 取舍 | 结论 |
|---|---|---|
| LangGraph `Send` API 真并行 fan-out | idiomatic，但增图复杂度 + 并发 rerank 撞 proxy；retry/SSE 重接 | 本期用 in-node 循环 + semaphore，更小侵入；`Send` 留作未来重构 |
| 每子问题独立 generate 子答案 → synthesize | 真 orchestrator-worker，但 N+1 次 mimo → 撞吞吐 + 爆 60s | **不做**（违背"0 LLM 增量"卖点）|
| 只改 rerank（保留单池，对池做 N 次 per-query rerank）| 更省改动 | 次选：单池 top-80 已可能在融合阶段丢弱 facet 的 chunk；per-query pool 更稳。先按主方案 |
| 全量启用（不 gate definition）| 简单 | **不做**：definition 要"找唯一权威条款"非"广度"，已调好的 title-boost 更优 |

## 9. 实现顺序建议

1. `round_robin_merge` 纯函数 + 单测（最易验证，先锁正确性）
2. settings 4 参数 + `state.candidates_by_query` 字段
3. `retrieve_node` map-reduce 分支 + 单测（触发门 + hyde 处理 + 回归）
4. `rerank_node` map-reduce 分支 + 单测（per-query 调用数 + facet 公平 + 失败隔离）
5. 集成测（多 facet 端到端）
6. `make lint`/`make test` 回归
7. eval multi_section 子集 before/after（§7.2）→ 数据达门槛 → 才建议人审开 flag

## 10. 文档同步清单（实现时，CLAUDE.md §8）

- `03-development/03-agent.md §4.5`（retrieve）+ `§4.6`（rerank）：补 map-reduce 分支说明
- `03-development/03-agent.md §2`：`AgentState` 加 `candidates_by_query` 字段
- 若暴露 settings 到 `.env.example`：同步 `01-infrastructure.md §2.4`
- 完成后在本文件追加"完成报告"（§4 模板）+ eval 数据

## 11. 待人决策（开工前）

1. **目标门槛**：ctx_recall ≥ 0.72 是否接受？（0.75 是 multi/formula 混合天花板，formula 受 ingestion 限制，multi 单独可更高）
2. **BUDGET 默认值**：12 vs 保守 10？（影响 generate 成本与信息倾倒风险）
3. **上线方式**：先灰度（flag 仅在 staging/eval 开），还是 eval 达标后直接生产开 flag？
4. **是否同时改 simple 路径**：本方案 simple 不受益（单 query）；若希望 simple 也多角度，需让 simple 也过 multi_query（另案，不在本期）。

## 12. 本次结论

- 仅产出实现文档，**未改任何代码**。
- 方案核心：复用现有节点 + 图拓扑，按 flag 在 complex（非 definition）路径把"伪 map"
  升级为"真 map-reduce"，**0 LLM 增量**，引用/SSE/retry 契约全不破坏。
- 下一步：按 §9 顺序实现，§7.2 eval 子集数据达 §7 门槛后再人审开 flag。

---

## 13. 完成报告（2026-06-02 实现）

> 决策（人定）：ctx_recall 门槛 **0.72** / `BUDGET=12` / **达标后直接上生产** / **不改 simple 路径**。
> 代码已全部落地并通过自测；flag 默认关（dormant），等 §7.2 eval 数据达标后再开。

### 13.1 交付物

| 文件 | 改动 |
|---|---|
| `backend/app/core/config.py` | +5 settings：`RETRIEVAL_MAPREDUCE_{ENABLED,PER_QUERY_POOL,PER_QUERY_TOPM,BUDGET,CONCURRENCY}`（默认 `False/30/4/12/3`）|
| `backend/app/agent/state.py` | `AgentState` +`candidates_by_query: list[list[RetrievedChunk]]` |
| `backend/app/retrieval/hybrid.py` | +`round_robin_merge()` 纯函数（轮转配额合并 + chunk_id 去重 + budget）|
| `backend/app/agent/nodes/retrieve.py` | 抽 `_fetch_dense_sparse()`；+`_mapreduce_retrieve()`；`retrieve_node` 按 §2.4 触发分流；single-pool 路径行为不变 |
| `backend/app/agent/nodes/rerank.py` | +`_base_queries()` / `_mapreduce_rerank()`；`rerank_node` 见非空 `candidates_by_query` 走 per-facet 重排 + 轮转合并 |
| `backend/tests/unit/retrieval/test_hybrid.py` | +7 `round_robin_merge` 单测 |
| `backend/tests/unit/agent/test_retrieve_node.py` | +6 map-reduce 单测（per-query 池 / hyde 非 facet / 4 个触发门排除）|
| `backend/tests/unit/agent/test_rerank_node.py` | +4 map-reduce 单测（per-facet 调用数 / 轮转 / 公平 / 无 reranker 兜底 / facet 失败隔离）|
| `backend/tests/integration/agent/test_mapreduce_qa.py` | +2 mock-graph 端到端（flag 开→多 facet reranked；flag 关→单池）|
| `docs/03-development/03-agent.md` | §2 加字段、§4.5/§4.6 加 map-reduce 分支说明 |

### 13.2 自测结果（全绿）

- `pytest -m unit`：**397 passed**（含本次 +17 单测）
- mock graph 集成（self_rag_retry / sse_events / checkpoint_ops / mapreduce_qa）：**11 passed**
- `ruff check` ✅ / `black --check` ✅ / `mypy`（5 改动文件）✅

### 13.3 自主决策记录（CLAUDE.md §4.3）

1. **图拓扑不改**：按 flag 在 `retrieve_node`/`rerank_node` 内部分流，而非新增节点/边——retry/SSE/引用契约零改动，simple/definition/tool 完全走旧路。
2. **hyde 不成 facet**：只汇入 flat 池（理想答案非"角度"，单独 rerank 无意义）。
3. **map-reduce 用独立 cache key**（payload +`mode:"mapreduce"`，value=`{flat, by_query}`）避免与 single-pool 串味。
4. **per-facet rerank 并发**用 `asyncio.gather` + `Semaphore(CONCURRENCY=3)`；单 facet 失败退回该 facet fused top-m，不阻塞其它。
5. **`round_robin_merge` 落在 `retrieval/hybrid.py`**（与 `rrf_merge` 并列、纯函数好测），全程在 retrieval-model 空间合并，末尾一次性转 StateChunk（类型干净，mypy 过）。

### 13.4 待人 / 剩余（§7.2 eval 门 + 生产开 flag）

flag 默认 **False**，线上行为零变化。开 flag 前必须跑 §7.2：

1. 用**本仓库代码**起一个 backend（`RETRIEVAL_MAPREDUCE_ENABLED=true`，连共享 Qdrant/LiteLLM/Redis），配 `EVAL_BACKEND_BASE_URL`+token。
2. 对 multi_section 8 题 live + ragas，flag **off（baseline）vs on** 各跑 **≥2 次**取统计量（judge 方差大）。
3. 门槛（§7）：ctx_recall **≥0.72** 且 faithfulness **不降**（≥0.82）→ 通过。
4. 通过 → 生产 `tgpp-api` 重建并置 `RETRIEVAL_MAPREDUCE_ENABLED=true` 上线（属生产部署，建议人盯）。

> ⚠️ 当前运行的 `tgpp-api` 容器是 `/home/s1yu` 部署的**线上镜像**，不含本次代码；
> eval 必须另起本仓库代码的实例，勿误判线上已生效。

### 13.5 上线记录（2026-06-02，人决策：先上线后 eval）

人改变推进顺序：**先 commit+push+部署上线（flag on），之后再跑 eval**，接受
"有问题再回退"的风险（当前单用户）。实际执行：

- commit `77c52f0` → push `origin/main`（`c561673..77c52f0`）。
- prod `.env` 追加 `RETRIEVAL_MAPREDUCE_ENABLED=true`（code default 仍 False；`.env` 已 gitignore）。
- 仅重建 `api` 镜像（`docker compose -f deploy/docker-compose.prod.yml build api` → `up -d api`），
  web 未动；`tgpp-api:prod` 13:29 重建。
- 验证：容器内 `RETRIEVAL_MAPREDUCE_ENABLED=true`、`/ready` 4 依赖全绿、docker health=healthy、
  `https://3gpp-everything.org/ready` 200、容器内已含 map-reduce 代码。

**eval 改为后置（post-hoc 验证 + 回退依据）**：线上现为 flag-on，无需再起对照实例——
直接对 live prod backend 跑 multi_section 子集 + ragas，与已存档 **v11 baseline（multi_section
ctx_recall 0.612 / faithfulness 0.851）** 比：

- 达标（ctx_recall ≥ 0.72 且 faithfulness 不降）→ 保持上线。
- 未达标 / faithfulness 掉 → 回退：`.env` 改回 `false`（或删行）→ `docker compose ... up -d api`
  即刻生效（无需重建镜像，flag 是运行时读取）。代码可留（dormant）。

> 回退最轻量路径：**只翻 `.env` flag + 重启 api**，秒级，不动代码/镜像。

### 13.6 eval 验证结果 + 最终决定（2026-06-02，保持 on）

对 live prod backend（`https://3gpp-everything.org`）跑 `multi_section` × 8 题（`hand-multi-001..008`，
与 v11 baseline 同口径），脚本 `eval/scripts/run_mapreduce_multisection{,_run3}.py`，
结果存 `eval-results/mapreduce-multisection-20260602T054430Z/`。run1/run2 默认 180s
（6 次 ragas TimeoutError → run2 faithfulness 4 题 null）；**run3 用 `RunConfig(timeout=600,
max_workers=2)` 救回超时**，再算 max-of-3（与 baseline 同口径）。

| 指标 | **max-of-3** | v11 baseline | Δ | 门槛 | |
|---|---:|---:|---:|---:|:--:|
| ragas_context_recall | **0.696** | 0.612 | **+8.4pp** | ≥0.72 | ✗ 差 2.4pp |
| ragas_faithfulness | **0.816** | 0.851 | −3.5pp | ≥0.82 | ✗ 差 0.4pp |
| ragas_answer_relevance | 0.876 | 0.775 | **+10.1pp** | — | ✓ |
| ragas_context_precision | 0.885 | 0.994 | −10.9pp | — | utilization 噪声大 |
| context_recall_section（子串）| 0.875 | — | — | — | |

**严谨复跑后仍未过 0.72/0.82 门槛**，但：
- map-reduce **达成设计目标**：ctx_recall 稳定 +8.4pp、ans_rel +10pp —— facet 挤占被修。
- 够不到 0.72 的**残余差距是 retriever 层硬 miss，非 facet 挤占**：`hand-multi-001`
  ctx_recall **三轮全 0.0**（召回 38.181 测试规范而非 38.211，baseline 已记的真·检索
  miss，map-reduce 修不了 spec 级 miss）；`hand-multi-004` 三轮 0.667/0.333/0.333。
  去掉这俩，其余 6 题 ctx_recall 近满分。
- faithfulness −3.5pp（0.816）是 BUDGET 8→12 多塞 chunk 的预期代价，噪声内、差门槛 0.4pp。

**人决定（保持 on）**：recall/ans_rel 净增、faithfulness 噪声内微降，当 net-positive 接受；
0.72 留给后续 **retriever/chunker 深修**（真正的杠杆）去凑，map-reduce flag 不动。

**后续杠杆（待办，非本任务）**：
1. 修 `hand-multi-001/004` 的 retriever miss（multi_section 召回错 spec，38.181↔38.211）——
   这是 ctx_recall 破 0.72 的关键；修完可复跑 `run_mapreduce_multisection*.py` 复核。
2. 若 faithfulness 想回 0.82+：试 BUDGET 12→10（`RETRIEVAL_MAPREDUCE_BUDGET`，运行时
   `.env` 可调）再评。
3. 复核口径：`run_mapreduce_multisection_run3.py`（max-of-3 + 600s 救超时）= 可信口径，
   后续 A/B 沿用。

> eval 临时账号 `eval_mr`（role=user）用完已 `is_active=false` 停用（连带 12h token 失效）；
> 将来复跑：`UPDATE users SET is_active=true WHERE username='eval_mr';` 再签 token 即可。
