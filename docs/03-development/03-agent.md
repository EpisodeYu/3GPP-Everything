# 03·03 - Agent 编排（LangGraph）

> 把"检索 + 推理"组装成可控、可流式、可观测的 Agent。最终被后端 SSE 接口消费。

## 0. M4 执行顺序（Agent 侧）

> 2026-05-17 拆解：M6 全量索引已完成（1270 specs / 394,859 chunks 在 `tgpp_chunks_voyage_d1024`；BM25 在 `/data/tgpp/bm25/voyage/by_spec/`），M4 直接消费。Agent 侧落 5 段，后端侧落 6 段（见 [`04-backend-api.md §0`](04-backend-api.md)）。按下表顺序推进，每段门禁全绿才能进下一段。

| 子里程碑 | 主要交付物 | 完成度门禁 |
|---|---|---|
| **M4.0** 共享底座 | `core/{config,logging,errors}` + `llm/litellm_client` + `db/` SQLAlchemy 全表 + `alembic` 初始迁移 + `retrieval/{dense,sparse,hybrid,rerank,cache}`（agent 与 backend 都依赖）| lint 全绿；`alembic upgrade head` 在干净 PG 通过；`test_retrieval_smoke.py` 用 3 个真实 query 能从 `tgpp_chunks_voyage_d1024` + BM25 拿回 top-50 |
| **M4.1** glossary 抽取 | 补 [`02-ingestion-and-indexing.md`](02-ingestion-and-indexing.md) 漏项：`ingestion/glossary/extractor.py` + `glossary-extract` CLI；从 `21.905` + 各 TS Definitions 章节抽 term 入 PG `glossary` | `glossary-extract --all` 跑完，PG `glossary` row 数 ≥ 1000；5 个高频术语（PDU Session / AMF / SMF / UPF / N1）能精确命中 |
| **M4.2** Agent 主干（simple fast path） | `agent/state.py` + 6 个 prompt md + 6 个节点（classify / rewrite / retrieve / rerank / generate / self_rag，grounding-only）+ `graph.py` simple 分支 | `pytest -m unit backend/tests/unit/agent/` 全绿；5/5 simple QA 端到端跑通；retrieve 节点 P50 ≤ 800ms |
| **M4.3** Agent 完整链路 | `nodes/hyde.py` + `nodes/multi_query.py` + complex 分支 + raw_lookup 分支 + self-RAG retry loop（`retry_count >= 2` 强制 accept） | simple / complex / raw_lookup 三路集成测全绿；构造低置信度场景验证不死循环 |
| **M4.4** 工具节点 | `tools/{web_search,glossary,toc,params}.py` + `graph.py` tool_dispatch 分支 | 4 工具均能命中真实数据；非 `explicit_tools` 场景不会触发 |
| **M4.5** Checkpoint + 取消/暂停 + Langfuse | `AsyncPostgresSaver` 接入 + `agent/checkpoint.py` 5 个纯函数 + AgentState 加 `paused`/`run_id` + 节点边界 `langgraph.types.interrupt`（M4.8 batch A.2 起统一）+ Langfuse `CallbackHandler` | §14 5 个 checkpoint 相关集成测全绿；Langfuse 本地能看到节点 span（人审一次）|

> 各段交付物完成后按 [`../00-vibe-coding-protocol.md §4`](../00-vibe-coding-protocol.md) 输出完成报告；触达 [`../../CLAUDE.md §5`](../../CLAUDE.md) 上报条件时停下问人。

## 1. 交付物

> 每条标 `[M4.x]` 关联 §0 子里程碑。完成后把 `[ ]` 替换为 `[x]`。

- [x] `[M4.2]` `backend/app/agent/graph.py`：导出编译好的 `tgpp_agent` (CompiledStateGraph) 与状态类型（simple fast path）；`[M4.3]` 扩展 complex / raw_lookup 分支
- [x] `[M4.2]` 简单链路节点（classify / rewrite / retrieve / rerank / generate / self_rag grounding-only）；`[M4.3]` 完整节点集（+ hyde / multi_query / self_rag retry）；`[M4.4]` 工具节点（web_search / glossary / toc / params）
- [x] `[M4.5]` PostgresSaver checkpointer：会话级持久化、可中断恢复
- [ ] `[M4.2/M4.3]` 流式 API：`astream_events` 输出节点状态 + token 增量 + 中间结果（hit chunks）
- [ ] `[M4.4]` 4 个工具节点：`web_search` / `glossary`（依赖 M4.1 数据） / `toc` / `params`
- [x] `[M4.5]` 中途取消：thread cancel 机制
- [x] `[M4.5]` Langfuse 集成（CallbackHandler 在 graph invoke 时注入）

## 2. State Schema

```python
# backend/app/agent/state.py
from typing import Annotated, Literal, Sequence
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel

class RetrievedChunk(BaseModel):
    chunk_id: str
    spec_id: str
    section_path: tuple[str, ...]
    section_title: str
    chunk_type: Literal["text","table","formula","figure"]
    content: str
    score_dense: float
    score_sparse: float | None
    score_rerank: float | None
    fused_score: float

class AgentState(BaseModel):
    # 输入
    user_input: str
    user_language: Literal["zh","en"] = "en"
    mode: Literal["qa"] = "qa"                        # raw_lookup 已下线；保留字段，恒为 qa
    explicit_tools: list[str] = []                   # 用户显式触发的工具，如 ["web_search"]

    # 多轮上下文
    messages: Annotated[Sequence[BaseMessage], add_messages] = []

    # 路由结果
    query_class: Literal["definition","procedure","tool","unknown"] | None = None
    complexity: Literal["simple","complex"] = "simple"   # 决定走单跳还是完整链路

    # 查询改写
    rewritten_queries: list[str] = []                # 多查询拆分后的 list
    hyde_doc: str | None = None

    # 检索
    candidates: list[RetrievedChunk] = []            # 取并集后排重 top-50
    reranked: list[RetrievedChunk] = []              # top-K（RERANK_TOP_K，默认 8）

    # 工具结果（按工具名 -> 结构化结果）
    tool_results: dict[str, object] = {}

    # 生成
    final_answer: str = ""
    citations: list[dict] = []                       # [{chunk_id, spec, section, span}]
    confidence: float = 0.0                          # self-RAG 自评

    # 自校验
    self_rag_verdict: Literal["accept","retry","unknown"] | None = None
    retry_count: int = 0

    # 监控
    trace_id: str | None = None                      # Langfuse trace id
    cancelled: bool = False
```

## 3. 状态图

```mermaid
stateDiagram-v2
    [*] --> classify
    classify --> fast_rag: complexity=simple AND class!=tool
    classify --> rewrite: complexity=complex
    classify --> tools: class=tool
    rewrite --> hyde
    hyde --> multi_query
    multi_query --> retrieve
    fast_rag --> retrieve
    retrieve --> rerank
    rerank --> generate
    tools --> tool_dispatch
    tool_dispatch --> web_search
    tool_dispatch --> glossary
    tool_dispatch --> toc
    tool_dispatch --> params
    web_search --> generate
    glossary --> generate
    toc --> generate
    params --> generate
    generate --> self_rag
    self_rag --> retrieve: verdict=retry AND retry_count<2
    self_rag --> insufficient: verdict=insufficient OR retry_count>=2
    self_rag --> [*]: verdict=accept
    insufficient --> [*]
```

> raw_lookup 模式已下线（2026-05-26）：曾有 `classify --> raw_lookup --> retrieve_only --> rerank_lookup --> [*]` 一路（纯检索、不调生成 LLM），现仅保留 qa。

## 4. 节点实现

### 4.0 LLM 调用约定（2026-05-28 修订 — mimo thinking 模式）

mimo 官方 API 行为对节点设计有两个关键影响：

1. **`mimo-v2.5` / `mimo-v2.5-pro` 默认 `thinking.type="enabled"`**：每次调用先消耗 `reasoning_content`，再吐 `content`；如果 `max_tokens` 不够大就会被 reasoning 吃光、`content` 为空（这是 M5 自动标题反复修不生效的根因）。
2. **思考模式下 `temperature` 被强制为 1.0**：即便传 `0` 也被静默忽略，单题 `reasoning_tokens` 跨 170–2429 大幅跳变；rewrite / title / self_rag 等"应当确定性"的节点会 run-to-run 输出不一样。

应对策略（**短输出 / 固定结构 / 需确定性**的节点全部关 thinking）：

| 节点 | thinking | max_tokens | 备注 |
|---|---|---:|---|
| `classify` | `disabled` | 默认 | JSON 路由，确定性要求高 |
| `rewrite` | `disabled` | 512 | 一句改写 |
| `multi_query` | `disabled` | 1024 | JSON 数组，sub_queries 不该跳变 |
| `self_rag` | `disabled` | 默认 | verdict JSON，retry 路径要稳定 |
| `session_title` | `disabled` | 512 | 短标题，同题同输出 |
| `history_compactor` summary | `disabled` | 800 | 事实压缩任务，不需 reasoning；同 older history 永远同 summary |
| `hyde` | **enabled** | 8192 | 理想答案段落需要推理质量 |
| `generate` | **enabled** | 流式无 cap | 终答质量最重要 |

实现机制：`LiteLLMClient.chat(thinking=...)` 把 `{"type":"disabled"}` 包进 `extra_body`（LiteLLM proxy 会过滤 OpenAI spec 外的 top-level 字段，必须走 `extra_body` 透传）。prod 容器实测 5 次同输入 → 1 个 unique 输出 ✅；5 个节点 reasoning_tokens 全部归 0，每次复杂查询省 ~3-5k token，单次延迟降 30-60%。

历史遗留：`classify` / `self_rag` 调用层曾传 `response_format={"type":"json_object"}`，但 mimo 官方文档 `response_format.type` "仅为 text"，是 LiteLLM 透传 + mimo 静默忽略；两处 prompt 已经强约束 JSON 输出 → 2026-05-28 一并移除 `response_format`，`LiteLLMClient.chat(response_format=...)` 形参保留作纯透传层（未来 json_schema 模型可用）。

### 4.1 `classify_node` — 路由 / 复杂度判定 / fast path 改写

- 模型：`mimo-v2.5`（轻量；`thinking=disabled`，详见 §4.0）
- 结构化输出（Pydantic schema）：

```python
class ClassifyOutput(BaseModel):
    query_class: Literal["definition","procedure","tool","unknown"]
    complexity: Literal["simple","complex"]
    detected_language: Literal["zh","en","mixed"]
    rewritten_query: str | None          # simple fast path 直接使用；complex 可为空
    needs_explicit_tools: list[str]   # 当 user_input 明确要求"搜索一下"等
    reason: str                        # 简短理由（≤ 50 字）
```

- 在 prompt 中明示判定标准：
  - `definition` = 单一术语 / 单一字段定义
  - `procedure` = 流程类查询
  - `tool` = 缩写表 / 章节目录 / 参数查询
  - 复杂度：包含多个 entity / 需要多文档证据 → complex
- simple 查询必须同时输出一个英文 `rewritten_query`，避免再单独调用 `rewrite_node`；只有 complex 才进入 `rewrite_node + hyde + multi_query`
- **定义题路由（2026-05-27）**：`query_class=definition` 几乎总被判 `complexity=simple`，但 IE/术语定义最需要精准命中那唯一的定义条款，单 query 召回最弱。`_after_classify` 把 `definition` 也路由到 complex 扩展链（`rewrite → hyde → multi_query`）提升召回——这同时让 `query_class` 真正参与路由（此前仅 `tool` 被消费）

> **定义类质量提升路线（2026-05-27 立项）**
> 背景：实测定义题答得不完整也不全对——根因是 `query_class` 此前只用于 `tool` 判定、定义题被丢进召回最弱的 simple 路径、语义检索把权威定义淹没在测试规范提及里、self_rag 只查 grounding 查不出"检索到外围而非定义"。四条改进：
> - **A（已做）**：定义题路由到扩展检索链（本节）。
> - **D（已做，eval 已验证）**：rerank 阶段 section_title 命中加权（§4.6）。2026-05-27 daily（56 题）实测：definition 类 section_recall **0.875→1.0**（8/8 全中，含此前全崩的 hand-def-002 由 0→1.0），forbidden 串味降低；详见 `docs/04-handoff/2026-05-27-ad-eval-findings.md`。
> - **B（待做）**：定义题**自动挂 glossary 工具**（不再要求用户显式请求），glossary 查词 + 检索混合，给出策划过的术语定义。
> - **C（待做）**：`query_class` 驱动**定义专用 generate prompt**——先给权威 ASN.1/条款定义，再补用法/跨规范引用。
> A+D 上线后应跑 eval 子集确认定义类 faithfulness/completeness 回升再决定是否上 B/C。

### 4.2 `rewrite_node` — 查询改写

- 模型：`mimo-v2.5`（`thinking=disabled` / `max_tokens=512`，详见 §4.0）
- 任务：
  - 中文输入 → 英文检索 query
  - 解词改 query（`5GS` → `5G System`）
  - 输出 1 个标准化 query（多查询拆分单独走 multi_query）

### 4.3 `hyde_node` — Hypothetical Document Embedding

- 仅 complex 走
- 让 LLM 假装写一段"理想答案的章节文本"（200-400 tokens），用它的 embedding 一起检索
- 模型：`mimo-v2.5-pro`（需要够强才生成的 hyde 有意义）

### 4.4 `multi_query_node`

- 把改写后的 query 拆 3-5 个**不同角度** sub-query
- 模型：`mimo-v2.5`（`thinking=disabled` / `max_tokens=1024`，详见 §4.0）
- 输出：`list[str]`

### 4.5 `retrieve_node` — Hybrid 检索

> **M4 决议（2026-05-17，Q2=A）**：M4 主干一开始即接 dense + BM25 sparse + RRF。M6 已落 BM25 持久化到 `/data/tgpp/bm25/voyage/by_spec/*.jsonl`，data 已 ready；不再走"先 dense-only、再增量加 sparse"的两步走方案。M6 dense-only baseline（spec R@10=0.580 / MRR=0.236）作为 ablation 对照存档，参见 [`../../eval-results/m6-retrieval-baseline.md`](../../eval-results/m6-retrieval-baseline.md)。

```python
async def retrieve_node(state: AgentState) -> AgentState:
    queries = state.rewritten_queries or [state.user_input]
    if state.hyde_doc:
        queries.append(state.hyde_doc)
    candidates = []
    for q in queries:
        dense = await dense_retriever.aretrieve(q, top_k=30)
        sparse = await sparse_retriever.aretrieve(q, top_k=30)
        candidates.extend(rrf_merge(dense, sparse))
    # 排重 + 元数据过滤
    unique = dedup_by_chunk_id(candidates)[:50]
    return state.model_copy(update={"candidates": unique})
```

- `dense_retriever`：`backend/app/retrieval/dense.py` 包 LlamaIndex VectorStoreIndex（Qdrant backend，复用 `tgpp_chunks_voyage_d1024`）
- `sparse_retriever`：`backend/app/retrieval/sparse.py` 包 `bm25s` 直接构建/加载，从 `/data/tgpp/bm25/voyage/` 持久化目录读：
  - **M8 起 — mmap fast path（默认）**：`bm25s.BM25.load(<dir>/index, mmap=True, load_corpus=False)` + `JsonlCorpus(corpus.jsonl)` lazy 访问。要求 ingestion 端跑过 `bm25-rebuild`（或新的 `finalize()` 默认带 build）。启动 < 1s，RSS 仅 ~250 MiB（mmap page 按需驻留）
  - **fallback**：找不到 `index/` 时退回旧 in-memory build（从 `by_spec/*.jsonl` 反序列化 list[dict] 再 `bm25s.tokenize+index`），实测 394k chunks 启动 30-60s、VmSize 峰值 ~2.5 GiB（启动日志会打 warning，应跑 `ingestion bm25-rebuild` 修复）
  - 加载路径可通过 `sparse_retriever.backend` 属性区分（`"mmap"` / `"legacy"`）；tokenize 参数（`stopwords="en"`）与 ingestion 端 `_TOKENIZE_STOPWORDS` 保持一致，改动需同步两侧
- RRF 融合：`score = sum(1 / (60 + rank_i))`
- 过滤：根据 `query_class` 选 `spec_id` 限定
- 缓存：`Redis tgpp:cache:retrieve:{sha256(query+filter)}` TTL 1h

### 4.6 `rerank_node`

> **M4 决议（2026-05-17，Q1=A）**：M4 主干 simple fast path 一开始就接 voyage `rerank-2.5`，与本节设计一致。M6 dense-only baseline 是 retrieval-only 的对照存档，rerank 接入后跑同一份 `eval/golden/v1.yaml` 做 ablation，预期 MRR / spec R@10 显著回升。M6 baseline 的 0.580 / 0.236 不作为 M4 验收阈值（端到端阈值由 M7 nightly eval 校验，见 [`06-evaluation-and-observability.md §7`](06-evaluation-and-observability.md)）。

```python
async def rerank_node(state: AgentState) -> AgentState:
    docs = [c.content for c in state.candidates]
    scores = await voyage_client.rerank(
        query=state.rewritten_queries[0] if state.rewritten_queries else state.user_input,
        documents=docs,
        model="rerank-2.5",
        top_k=s.RERANK_TOP_K,   # 默认 8（2026-05-27 由 5 提到 8）
    )
    reranked = sorted_by_rerank(state.candidates, scores)[: s.RERANK_TOP_K]
    return state.model_copy(update={"reranked": reranked})
```

- 缓存：同 retrieve（`Redis tgpp:cache:rerank:{sha256(query+top_chunk_ids)}` TTL 1h）
- **定义题 section_title 命中加权（2026-05-27）**：`query_class=definition` 时，先取**全部**候选的 rerank 结果（Voyage 按候选数计费，放宽 `top_k` 只影响返回截断，免费），再对 `section_title` 命中查询里 IE/专名 token（hyphenated / CamelCase / 全大写，如 `PDSCH-Config`、`AMF`）的 chunk 加 `_DEFINITION_TITLE_BOOST`（默认 `0.1`），最后截到 `RERANK_TOP_K`。目的：把"标题即该 IE"的定义条款顶到只是**提及**该 IE 的测试/一致性规范之上。boost 权重与 token 抽取规则是启发式，`0.1` 经 2026-05-27 daily eval 验证有效（definition section_recall 0.875→1.0），后续若调权重需复跑 eval；只作用于 definition 类，不影响 procedure/complex 路径

### 4.7 `generate_node` — 最终生成

- 模型：`mimo-v2.5-pro`（streaming=True）
- Prompt 要点（见 §5 prompt 库）：
  - 严格 grounding：仅基于 `reranked` 内容
  - 引用格式：`[spec_id §section_path ¶offset]`
  - 输出语言：`state.user_language`
  - 公式保留 LaTeX
  - 篇幅：**完整性驱动，不设固定字数**（rule #6，prompt v3 → v4，2026-05-27/28）——
    覆盖证据里所有相关规范要点，该长就长，但不注水；配合 `RERANK_TOP_K` 5→8 让复杂/
    跨规范问题答得更全
  - **v4 grounding 护栏（2026-05-28）**：v3 "完整性驱动" 实测过冲成信息倾倒——
    def-007 ragas faithfulness 0.8→0.19，模型把 §5.2.2.5 所有 Rel-18/19 边角条款
    全倒出、远超 cited chunk 支撑；可比对 ragas（n=37）faithfulness 0.578→0.499，
    definition 类 0.658→0.441 为重灾区。rule #6 v3→v4：**只答所问、每句须有 cited
    chunk 支撑、禁堆砌问题没问的 edge case**，保留完整性但加 grounding 上限。
  - **chunk header 反诱导（prompt v5，2026-05-28）**：38.331 等 spec 的 IE/ASN.1 章节
    `section_path` 为空，chunker `_section_header` 给 chunk content 注入 `[spec §
    *IE-name* information element]` 头，长得**和 prompt 要求的 citation 格式一样**，
    LLM 会 verbatim 抄回来形成无效引用（前端 chip 渲染但跳转必 404）。v5：(a) 元数据行
    空 `section_path` 显式标 `<none>` 而不是裸空串；(b) hard rule #2 加 bullet 明确
    "chunk body 顶部的方括号是 chunker artifact，不是 citation 模板，禁止 verbatim
    复制；空 `<none>` 时只写 `[spec_id]`"

- 输出后用正则提取 `[xx §xx]` 写入 `state.citations`。**v5 同步把 `parse_citations`
  改为三段 fallback**（strict dotted-clause → fuzzy by section_title → spec-only），
  让 LLM 引用格式漂移时仍能拿到 chunk_id 给前端 chip 用：
  - 正则放宽到 `[spec(?: §sect)?]`：`[38.331]` 也认（v5 prompt 在 chunk 无 clause
    时要求的形态）
  - section 段含 `*` / 空格 / IE 名等非 dotted-clause 形态 → 与 `chunk.section_title`
    归一化后包含匹配（去 `*` / HTML 标签 / 大小写），实测能捞回 38.331 IE chunk
  - 都匹不到 → 同 spec 第一条兜底（保持原行为）
  - 前端 `jumpToReader` 同步收紧：非 dotted clause section 不跳 `/reader/{spec}/{?}`
    （必 404），改跳 spec overview + SnackBar 提示

- **tool 路径 `_render_tool_results` sanitize（v5）**：raw 3GPP markdown 里含
  `<b>...</b>` HTML 标签 / `*xxx*` 强调符 / 表格分隔行，preview 字段 240 字符硬切
  又常切在表格中间，前端 `flutter_markdown_plus` 不解析 HTML 标签 → 原样回显乱码。
  `_sanitize_preview(text, max_chars=180)` 在 params / glossary / web_search snippet
  渲染前清洗：去 HTML 标签、解包强调符、去表格分隔行、压换行与连续管道符

### 4.8 `self_rag_node` — 自校验

- 模型：`mimo-v2.5`（轻量；`thinking=disabled`，详见 §4.0）
- 输入：`user_input + reranked.content + final_answer`
- 结构化输出：

```python
class SelfRagOutput(BaseModel):
    faithful: bool                # 答案是否完全 grounded
    coverage: float               # 0-1，关键事实覆盖度
    confidence: float             # 0-1
    verdict: Literal["accept","retry","insufficient"]
    missing_aspects: list[str]    # 缺哪些 facet（驱动 retry 时新增 query）
```

- 路由：
  - `accept` → END
  - `retry` 且 `retry_count < 2` → 把 `missing_aspects` 改写为新 query，回 `retrieve_node`
  - `insufficient` 或 retry 后仍不足 → 生成最终回答："未在已索引 3GPP 文档中找到 …"，END

**性能策略**：

- simple fast path 不默认跑完整 self-RAG retry 循环；只做轻量 citation/grounding check（同一 `self_rag_node`，但 `allow_retry=false`）。
- complex 查询仍做 self-RAG 校验，最多 retry 2 次后强制收敛，避免成本失控。
- 低置信度 simple 查询（rerank top score 低、引用不足、生成答案缺引用）才升级到完整 self-RAG retry。

### 4.9 工具节点

`backend/app/tools/`：

#### `web_search`
- 用 Tavily SDK，仅当 `state.explicit_tools` 含 `"web_search"`
- 返回 markdown + url 列表
- 答案前缀强制加："以下内容来自 Web 搜索，未经 3GPP 验证："

#### `glossary`
- 查"缩写/术语表"：从 PG `glossary` 表（**M4.1 ingestion 子任务**从 `21.905` + 各 TS Definitions 章节抽取构建——这是补 [`02-ingestion-and-indexing.md`](02-ingestion-and-indexing.md) 漏项，原 plan 写"M2 期间抽取"但 M2 实际未做）
- 命中后短答 + 给所在 spec 引用
- 工具节点本身在 M4.4 实现；若 M4.1 尚未交付，工具节点先返回空结果并打 warning，不阻塞 M4.4 集成测的"工具被正确调用"断言

#### `toc`
- 章节目录查询（"列出 38.331 §5.3 所有子节"）
- 从 PG `chunks_meta` 按 `spec_id + section_path` 前缀查询

#### `params`
- IE / 字段查询（"X 字段在哪些 spec 出现过"）
- 走 BM25 全文检索（精确字段名 + 限定 `chunk_type` 为 text/table）

### 4.10 ~~`raw_lookup_node`~~（已下线）

raw_lookup 模式于 2026-05-26 下线（产品决策：只保留 QA + 在回答里把协议章节做成可跳转超链接）。
原行为：mode = raw_lookup 时 retrieve → rerank → 返回 top chunks、不调生成 LLM。
现 graph 不再按 mode 分流，START 直接进 classify，rerank 一律进 generate。

## 5. Prompt 库管理

`backend/app/agent/prompts/`，单独 markdown 文件：

```
prompts/
├── classify.md
├── rewrite.md
├── hyde.md
├── multi_query.md
├── generate_qa.md
├── self_rag.md
└── tools/
    ├── web_search_prefix.md
    └── ...
```

- 每个 prompt 顶部 frontmatter 写 `version: 1` + `notes:`，迭代留痕
- 加载用 `jinja2.Template`
- 集成测覆盖：`test_prompts_render_without_undefined_vars`

## 6. PostgresSaver Checkpointer

```python
from langgraph.checkpoint.postgres import AsyncPostgresSaver

checkpointer = AsyncPostgresSaver.from_conn_string(
    DATABASE_URL.replace("+asyncpg",""),     # langgraph 用 psycopg
)
await checkpointer.setup()                    # 建 schema langgraph_*

graph = builder.compile(checkpointer=checkpointer)
```

- thread_id = `session_id`（来自后端，由 user_id + uuid 构成）
- 每个 session 的多轮消息天然累积在 messages 状态
- 支持"中途取消"：后端在另一个连接里 `graph.aupdate_state(config, {"cancelled": True})` + thread 内每个节点开头检查 `state.cancelled`

### 6.1 多轮历史压缩（M4.7 Q10 决策）

长会话累计后，原始 message 列表直塞 system prompt 会撑爆 mimo-v2.5-pro 的 context。M4.7 实装一步到位的混合策略：

- 取最近 **N=6** 条 user/assistant 消息（约 12 轮）原文拼入 LangGraph state；
- 第 N 条之前的所有消息按出现顺序 batch 化交 `mimo-v2.5`（**非 pro**，省成本）做 summary，结果以单条 `system: <summary>` 注入 prompt 头部；**2026-05-28 起 summary 调用 `thinking=disabled`**（详见 §4.0），同 older history 永远同 summary，避免 Redis cache 落库后"表面稳定但内涵不可复现"——重放调试 / 清 cache 重跑会得到不同 summary 影响下游 LLM 决策追踪；
- summary 缓存：Redis `tgpp:cache:history_summary:{session_id}:{last_summarized_message_id}`，TTL 24h；
- 触发条件：会话 `message_count > 8` 时启用 summary 路径，否则只取最近 N 条；
- 实现位置（约定）：`backend/app/agent/utils/history_compactor.py`，由 `build_graph` 的 deps 注入，使 LangGraph 节点对 history 字段直接消费。

> 详见决策档 [`../04-handoff/2026-05-17-m4.6-m4.9-decisions.md`](../04-handoff/2026-05-17-m4.6-m4.9-decisions.md) §一 Q10 与 §2.2 M4.7。N=6 / 阈值 8 / 缓存 TTL 24h 属 Agent 自主决策范围（CLAUDE.md §4.3）；如线上观察到 summary 失真或成本偏高，在 M5 PR review 阶段调即可，不再走人审。

## 7. 流式输出协议

LangGraph `astream_events(v="v2")` 产出事件序列。后端把这些事件**重新映射**到我们对前端友好的 SSE event 类型：

| LangGraph event | 后端 SSE event | payload |
|----------------|---------------|---------|
| `on_chain_start` (node) | `node_start` | `{node, ts}` |
| `on_chain_end` (node) | `node_end` | `{node, duration_ms, summary}` |
| `on_chat_model_stream` (generate) | `token` | `{delta}` | （历史路径；canned-graph 集成测仍保留） |
| `generate` 节点内 `adispatch_custom_event("token", {"delta": ...})`（**f22cb23, 2026-05-28**） | `token` | `{delta}` | 真路径：generate 改用 `LiteLLMClient.chat_stream()` 拼 chunk，通过 custom event + writer 双通道 emit；SSE 路由 `on_custom_event name=="token"` 透传。LangGraph `astream_events("v2")` 的 `on_chat_model_stream` 只识别 LangChain `BaseChatModel.astream`，自定义 httpx 客户端走不到 → prod 表现 "等 final 一次性吐全文"；流式失败兜底回退到非流式 `chat()`。 |
| `retrieve` 节点内显式 `astream_writer({"type":"chunks_hit"})` | `chunks_hit` | `[{chunk_id, spec_id, section_path, preview}]`（**不带 rerank_score**） |
| `rerank` 节点内显式 `astream_writer({"type":"chunks_rerank"})` | `chunks_rerank` | `[{chunk_id, spec_id, section_path, preview, rerank_score}]`（top-K 重排后） |
| graph 完成 | `final` | `{answer, citations, confidence}` |
| 首轮自动标题（非 LangGraph 事件，见下） | `title` | `{session_id, title}` |
| graph error | `error` | `{message}` |

> **首轮自动标题**（`title` 事件）：会话以空 `title` 创建；当本轮是首轮（`session.title`
> 仍为空）且成功产出答案时，后端用 LIGHT 模型（`LLM_LIGHT_MODEL`，`thinking=disabled` /
> `max_tokens=512`，详见 §4.0）把用户首个问题压成一句短标题，回写 `session.title` 并
> emit `title` 事件让前端 sidebar 即时更新。任何失败都 swallow（标题保持空，前端
> fallback「新会话」，下一轮再尝试）。resume / fork 续跑路径不触发。
>
> **2026-05-28 起 autotitle 与 agent 并发起跑**：SSE 入口立即 `asyncio.create_task`
> 启动 title LLM，主循环每次事件后 poll；title 通常在第一个 `token` 之前就到。agent
> 跑完时若 title 仍未好，2s 短超时兜底 + cancel，不阻塞 `end`；CancelledError 路径
> 同步 cancel title task，避免后台 LLM 烧 token。DB 写仍在主 task 串行
> （AsyncSession 非 task-safe）。
>
> **fallback 兜底（f22cb23, 2026-05-28）**：`_get_title_client` 增加 lazy 第三档
> fallback —— 若 `app.state._agent_deps.llm` 未就绪（lifespan 上游不可达 swallow
> 异常时），触发 `_build_default()` 后从 graph 模块 cache 里读 `deps.llm`，与 chat
> 路由的 graph fallback 同源；任何 ImportError / 构造失败 swallow，标题降级为
> no-op（与"自动标题失败不影响主流程"语义一致）。修了 prod 表现 "代码路径在跑但
> 前端永远收不到 title 事件"。
>
> 实现见 `app/services/session_title.py` + `app/api/v1/chat.py`。

> M4.7 Q6/Q7 决策（详见 [`../04-handoff/2026-05-17-m4.6-m4.9-decisions.md`](../04-handoff/2026-05-17-m4.6-m4.9-decisions.md) §一 Q6/Q7）：
>
> - `chunks_hit` 在 retrieve 完成后立即 push（top-50 预览，无 rerank_score），让前端尽早渲染候选；
> - `chunks_rerank` 在 rerank 完成后 push（top-K + rerank_score），覆盖前端的候选展示，用于引用 chip 染色（O13）。
> - 前端契约：收到 `chunks_rerank` 时按 chunk_id 合并/覆盖 `chunks_hit` 那批；未收到 rerank 事件（如 rerank 跳过）则保留 chunks_hit 原貌。

## 8. Langfuse 集成

```python
from langfuse.callback import CallbackHandler

handler = CallbackHandler(
    public_key=..., secret_key=..., host=...,
    session_id=session_id,
    user_id=user_id,
    metadata={"app": "tgpp", "mode": state.mode},
)
async for event in graph.astream_events(..., config={"callbacks":[handler]}):
    ...
```

- 每次 graph 调用一个 trace；每个节点一个 span
- 在 `generate_node` 前 `handler.flush()` 一次，确保 token 流的 trace 能 quasi-实时看到

## 9. 测试策略

- **单元测**：每个节点独立、mock LLM 与 retriever，断言状态变换
- **集成测**：从 user_input 直跑到 final answer，校验：
  - simple/complex 各 5 题，验证路由分支正确
  - tool 触发：用例 `"搜一下 38.331 最新版本进度"` → web_search 被调用
- **eval 测**（M3+）：金标准集 + Ragas

## 10. 性能预算

| 节点 | 模型 | 期望耗时 | 备注 |
|------|------|---------|------|
| classify | mimo-v2.5 | 1-2s | 短输入短输出 |
| rewrite | mimo-v2.5 | 1-2s | |
| hyde | mimo-v2.5-pro | 3-5s | 仅 complex |
| multi_query | mimo-v2.5 | 1-2s | 仅 complex |
| retrieve | - | 0.3-0.8s | dense + sparse 并发 |
| rerank | voyage rerank-2.5 | 0.5-1s | |
| generate | mimo-v2.5-pro | 5-30s | streaming；50-1000 tokens |
| self_rag | mimo-v2.5 | 2-3s | |

**simple fast path**：classify（含 rewrite）+ retrieve + rerank + generate + 轻量 grounding check，目标 P95 < 15s。
**complex 链路**：rewrite + hyde + multi_query + retrieve/rerank + generate + self-RAG（最多 1 次 retry），目标 P95 < 60s。

与需求 §4.1 一致（raw_lookup 模式已下线）。

## 11. 中途取消机制

```mermaid
sequenceDiagram
    Client->>Backend: POST /chat (start)
    Backend->>LangGraph: astream_events(config={thread_id})
    LangGraph-->>Backend: events ...
    Backend-->>Client: SSE events
    Client->>Backend: DELETE /chat/runs/{run_id}
    Backend->>LangGraph: aupdate_state({cancelled: true})
    Note over LangGraph: 下个节点开头检测 cancelled
    LangGraph-->>Backend: graph ends gracefully
    Backend-->>Client: SSE event: cancelled
```

每个节点开头 1 行（M4.8 batch A.2 起统一改用 `langgraph.types.interrupt`，旧的 `raise NodeInterrupt(...)` 已全部迁移）：

```python
from langgraph.types import interrupt
if state.cancelled: interrupt({"reason": "cancelled by user"})
```

> **取消 vs 暂停的区别**：取消 = 标记 `cancelled=true`，graph 在下一节点终止并保留落盘 checkpoint，但**会话状态视为终止**——下次再问从头跑；暂停 = 同样停在下一节点边界，但**保留为"可恢复 run"**——下次进会话从最后一个 checkpoint 直接续跑后续节点。详见 §12。

## 12. Checkpoint 操作集（暴露给后端）

LangGraph `PostgresSaver` 在每个节点输出后自动落 checkpoint，`thread_id = session_id`。Agent 层向 `04-backend-api.md` 暴露以下 5 个操作（封装为 `backend/app/agent/checkpoint.py` 的纯函数，后端 API 直接包装）：

| 操作 | LangGraph 调用 | 行为 |
|------|---------------|------|
| `list_checkpoints(session_id)` | `tgpp_agent.aget_state_history(config={thread_id})` | 返回该会话所有 checkpoint 的 (checkpoint_id, parent_checkpoint_id, created_at, last_node, message_id) 列表，按时间倒序 |
| `pause_run(session_id, run_id)` | `aupdate_state({paused: true, run_id})` | 当前 run 在下一节点边界停止；checkpoint 保留；run 状态机置 `paused` |
| `resume_run(session_id)` | `astream_events(input=None, config={thread_id})` | 不传新 input，LangGraph 从最后 checkpoint 续跑剩余节点；前端继续接 SSE |
| `fork_from(session_id, checkpoint_id, new_user_message)` | `aupdate_state(config={thread_id, checkpoint_id}, values={...})` + `astream_events(input=new_user_message)` | 从指定 checkpoint 起新分支：原会话标记为只读历史，新建 `session_id'` 继承 checkpoint 链 + 新 user message 触发后续节点跑 |
| `rollback(session_id, last_n)` | 删除最近 `n` 个 checkpoint（同时删 PG `messages` 表对应行） | 恢复到 N+1 轮末状态；不可逆 |

**State 增补字段**（落到 §2 AgentState）：

```python
paused: bool = False              # pause_run 设置；resume_run 清除
run_id: str | None = None         # 当前活跃 run 标识，pause/cancel 用于鉴别
```

**节点边界检测**：与取消机制共用同一段，扩展为：

```python
from langgraph.types import interrupt
if state.cancelled: interrupt({"reason": "cancelled by user"})
if state.paused:    interrupt({"reason": "paused by user"})  # 区别：paused 不清空 state，可恢复
```

**Fork 实现要点**：

- LangGraph 不支持"同 thread_id 多分支并存"，因此 fork 通过**新建 thread_id** 实现：拷贝 checkpoint 链到新 `session_id'`，原 `session_id` 在 PG `sessions` 表标 `status=archived_branch`，前端会话列表分组显示
- 不做多分支可视化（详见需求 §3.9 不在范围）

## 13. 风险与排雷

| 风险 | 触发 | 应对 |
|------|------|------|
| LLM 不遵守严格 grounding，幻觉补齐 | self_rag 检测不到 | prompts/generate_qa.md 多轮迭代；self_rag 设独立模型避免同源偏差 |
| self_rag 死循环 | 一直 retry | `retry_count >= 2` 强制 accept |
| 工具节点权限滥用 | Agent 自作主张调 web_search | 严格判断 `explicit_tools`，prompt 中明示"未列入 explicit_tools 不得调用" |
| Voyage 海外延迟波动 | 网络抖动 | tenacity + 30s timeout + fallback 到 Jina rerank（已在选型文档） |
| PG checkpointer 锁竞争 | 小规模多用户同时发起长任务 | thread_id 按 session 隔离；必要时调连接池与 checkpoint 写入频率 |
| 暂停的 run 永不恢复堆积 checkpoint | 用户暂停后忘了 | 后台任务每天清理 `paused` 状态超过 N 天的 run；同时 `sessions` 表展示"暂停中"标记 |
| Fork 后用户在新旧分支间来回切换混乱 | UX 不清晰 | 会话列表按"主线 / 分叉历史"两组分开；archived_branch 加视觉灰度 |

## 14. 验收清单

> 按 §0 子里程碑分组。标注：`[auto]` = Agent 自跑可判定；`[human]` = 需要人审（外部 trace 可视化、回答质量）。同一段全绿才能进下一段。

### M4.0 共享底座

- [x] `[auto]` `pytest -m unit backend/tests/unit/{core,llm,db,retrieval}/` 全绿 — 2026-05-17，47 passed
- [x] `[auto]` `alembic upgrade head` 在干净 PG 通过；`alembic downgrade -1` 也可 — 2026-05-17 通过
- [x] `[auto]` `test_retrieval_smoke.py`：3 个真实 query 能从 `tgpp_chunks_voyage_d1024` + BM25 拿回 top-50 合并结果 — 2026-05-17 通过（88.7s，394k chunks）

> 2026-05-17 完成。详见 [`04-backend-api.md §12 M4.0`](04-backend-api.md)。

### M4.1 glossary 抽取

- [x] `[auto]` `pytest -m unit ingestion/tests/unit/test_glossary_extractor.py` 全绿 — 2026-05-17 通过（18/18，3.4s）
- [x] `[auto]` `ingestion glossary-extract --all` 跑完，PG `glossary` 表 row 数 ≥ 1000 — 2026-05-17 通过（1270 specs，34 154 行）
- [x] `[auto]` 5 个高频术语（PDU Session / AMF / SMF / UPF / N1）`normalized_term` 精确命中 — **4/5 通过**：PDU Session(3)/AMF(65)/SMF(55)/UPF(38) 命中；N1 在任何 §3.1 Definitions / §3.2 Abbreviations 章节都未作为独立术语出现（仅 "N1 mode" / "N1 NAS signalling connection"），属语料层面缺口，不阻塞 M4.4 工具节点接入

> 2026-05-17 完成。备注：21.905（"Vocabulary for 3GPP Specifications" TR）不在 GSMA/3GPP HF 数据集内（`HfApi.list_repo_files` 0 命中），运行器中 `_ALWAYS_INCLUDE_SPEC_IDS = {"21.905"}` 的特判保留作为占位，等数据集就绪后自动生效。当前 34 154 行全部来自 TS 5G 白名单（series 21–38）的 1270 篇 spec。

### M4.2 Agent 主干（simple fast path）

- [x] `[auto]` `pytest -m unit backend/tests/unit/agent/` 全绿（每节点独立 mock LLM + retriever）— 2026-05-17 通过（41 passed；backend 整体 92 passed）
- [x] `[auto]` `pytest -m integration backend/tests/integration/agent/test_simple_qa.py`：金标准 5 题 simple QA 端到端 — 2026-05-17 真实环境（LiteLLM proxy + Qdrant `tgpp_chunks_voyage_d1024` + BM25 394k chunks）实跑通过：5/5 都拿到 final_answer + reranked + citations + `verdict=accept`，`confidence` 0.85-0.95
- [x] `[auto]` retrieve_node P50 latency ≤ 800ms（dense + sparse + RRF）— 2026-05-17 真实环境实跑通过（`test_retrieve_node_p50_latency_under_800ms` 5 query 全 ≤ 800ms 守约）；单测 wrapper overhead ≤ 50ms（mock retriever）

> 2026-05-17 完成。M4.2 simple fast path 端到端在单测 + 真实环境双重验证通过。
>
> 真实环境（5 题 simple QA from golden v1.yaml）诊断指标（informational，非验收门禁）：
> - spec hit in citation: 2/5（def-003 / def-004 引用命中金标 expected_specs）
> - spec hit in retrieval (top-5): 3/5（def-002 / def-003 / def-004 dense+sparse+rerank top-5 含金标 spec）
> - def-002 retrieval 召回了 38.473（金标）但 LLM 引用了 38.401（同属 NG-RAN F1AP 群）；def-001 / def-005 retrieval 未召回金标 spec，属 retrieval 召回质量问题，由 M7 nightly eval 严格评测（`docs/06-evaluation-and-observability.md §7`），**不**作为 M4.2 验收门禁
> - 端到端时延：5 题平均 ~28s/题（classify + retrieve + rerank + generate + self_rag）；retrieve_node P50 单点 ≤ 800ms 守约
>
> 自主决策记录（CLAUDE.md §4.3）：
> - AgentState `messages` 用 `Annotated[list[BaseMessage], add_messages]` reducer，与 §2 文档保持一致；M4.2 单轮已 work，M4.5 加 PostgresSaver 后无缝多轮
> - DI 通过 `AgentDeps` 容器 + `functools.partial(node, deps=deps)` 闭包注入；测试可直接 `await node(state, deps=stub_deps)` 不需要全局 mock
> - 节点边界 `cancelled/paused` 检测在每个节点开头 1 行 `langgraph.types.interrupt`（M4.5 落地，M4.8 batch A.2 起统一），与 §11/§12 一致；M4.2 simple 不消费 paused 但保留断言以备 M4.5
> - 引用抽取用 `re.compile(r"\[(spec)\s*§(sect)\]")`，对 chunk.section_path **前缀匹配**：LLM 引用 §5.3 而 chunk 是 §5.3.5.1 也算命中（避免严格匹配漏召回）
> - 顺手修了 `app/core/config.py` 一处 .env 解析 bug：`ALLOWED_ORIGINS` CSV/单值/JSON 三种写法都能解析（`Annotated[..., NoDecode]` + `field_validator`），并把 `env_file` 改为多路径搜索（cwd + 项目根），不再要求一定从项目根跑 pytest。M4.0 完成后有人在 `.env` 加了 CSV 写法直接打挂 Settings()；这次顺路修掉，新增 4 条单测覆盖（`test_allowed_origins_*`）。属于 §3 surgical changes 边界内（实际阻塞 M4.2 集成测启动）

### M4.3 Agent 完整链路

- [x] `[auto]` complex QA 端到端（金标准 5 题）
- [x] `[auto]` raw_lookup 模式不调用生成 LLM（断言 LLM mock 调用次数 = 0）
- [x] `[auto]` self-RAG retry 上限：构造低置信度场景，验证不会死循环（`retry_count >= 2` 强制收敛）
- [x] `[auto]` 流式 SSE event 序列符合 §7 表（集成测断言事件顺序与字段）

> **2026-05-17 完成 M4.3**：
> - 交付物：`nodes/hyde.py`（HyDE 用 `LLM_AGENT_MODEL`，失败优雅降级 `hyde_doc=None`）、`nodes/multi_query.py`（JSON array 解析 + prose fallback + 1+5 cap + 大小写去重）、`graph.py` 加 `build_graph(deps)` 编排三路（raw_lookup / simple / complex）+ self-RAG retry（`_RETRY_CAP=2` 强制收敛）、`retrieve.py` 双轨 emit `chunks_hit`（`get_stream_writer()` + `adispatch_custom_event`，让 `astream(stream_mode="custom")` 和 `astream_events(v2)` 都能看到）、`self_rag.py` 在 `verdict=retry` 时 `retry_count+=1` 并 append `missing_aspects` 到 `rewritten_queries`（大小写去重）
> - 测试：unit 103 / mock integration 5 全绿；`test_complex_qa.py` 真实环境 5 题跑 347s，5/5 跑通完整 complex 链路（classify→rewrite→hyde→multi_query→retrieve→rerank→generate→self_rag），retrieval/citation 严指标按文档由 M7 nightly eval 跑（当前 spec hit 1/5）
> - 自主决策记录（CLAUDE.md §4.3）：(1) chunks_hit payload 拆 `spec_id` + `section_path` 而非合并 `spec` 字段，让 backend §7 表 SSE 输出层重映射成 `spec="23.501 §6.3.1"`；(2) multi-query 上限按 prompt 默认 5（cap = 1 primary + 5 sub-queries）；(3) retry 只重跑 retrieve→rerank→generate→self_rag，不重新 classify/rewrite/hyde/multi_query，避免 rewritten_queries 被反复覆盖
> - 留给人审：无关键决策项；M4.4 工具节点接入后建议复跑 complex_qa 看 cited spec hit 是否提升
> - 剩余风险：spec hit 严指标偏弱属预期范围（文档明确指 M7 nightly eval 跑），不影响 M4.3 验收

### M4.4 工具节点

- [x] `[auto]` 4 工具节点显式触发集成测（web_search / glossary / toc / params 各一个用例）
- [x] `[auto]` glossary 工具节点能命中真实数据（依赖 M4.1，断言至少 1 个高频术语返回非空）
- [x] `[auto]` 非 `explicit_tools` 场景下工具节点不会被调用（prompt 守约 + 路由守约双保险）

> **2026-05-17 完成 M4.4**：
> - 交付物：`app/tools/{web_search,glossary,toc,params}.py` 四个工具（统一签名 `async def(state, *, deps) -> dict`）+ `TOOL_REGISTRY` 注册表；`app/agent/nodes/tool_dispatch.py` 节点按 `state.explicit_tools` 并发跑工具（`asyncio.gather`），异常隔离；`graph.py` 加 `query_class=="tool"` 路由 `classify → tool_dispatch → generate → self_rag → END`（不走 retrieve/rerank）；`generate.py` 增 `_render_tool_results()` 把 4 类工具结果落地到 `final_answer`（web_search 强制带 §4.9 安全前缀）；`AgentDeps` 加 `db_sessionmaker` 字段供 glossary/toc 用
> - prompt：`prompts/classify.md` 扩 `needs_explicit_tools` 允许值至 4 类工具 + 中英文意图映射规则（query_class=tool 时**强制**至少填 1 项）
> - 测试：unit 127 / mock integration 11（含 4 工具显式触发 + 工具异常隔离 + 非 explicit 不触发）；真实环境 PG glossary 单独跑通（AMF 命中 203 行，normalized_term 已 lowercase）；make lint 全绿
> - 自主决策记录（CLAUDE.md §4.3）：(1) tool 路径不走 retrieve/rerank，由 generate_node 直接渲染 tool_results；该路径 LLM 调用数 = classify+self_rag（generate 不再调 LLM，保持成本可预测）；(2) 工具调度并发跑（`asyncio.gather`），任一工具异常吞成 warning 不阻塞其它工具；(3) glossary `normalized_term` 对齐 ingestion 端 `lower()` 约定，候选 term 用 `_TOKEN_RE` 抽 + stopwords 过滤，避免 "What/is/the" 等噪音匹配
> - 留给人审：tool 路径下 generate_node 渲染是模板化的（非 LLM 综合），如需更自然的回答可在 M4.5+ 加 prompt template；非 explicit_tools+query_class!=tool 但用户其实需要工具的场景（如 procedure 查询附带搜网）走完整 retrieve 路径，工具不触发——符合"显式触发"硬规
> - 剩余风险：classify 的 `needs_explicit_tools` 4 类映射依赖 LLM 严格遵守 prompt 规则；如出现 LLM 误分类把 definition/procedure 标成 tool 会跳过 retrieve；M7 nightly eval 加路由分类 confusion matrix 监控

### M4.5 Checkpoint + 取消/暂停 + Langfuse

- [x] `[auto]` `pytest -m integration backend/tests/integration/agent/` 补齐：
  - 中途取消
  - **暂停 → 关进程 → 重启 → 恢复续跑**
  - **从历史 checkpoint fork 出新会话 + 老会话变只读（status=archived_branch）**
  - **rollback 最后 N 轮 messages + checkpoints 一致性**
- [ ] `[human]` Langfuse 中能看到完整 trace（每个节点 span + token stream）—— Langfuse Cloud 账号由人创建，trace 实际效果由人确认

> **2026-05-17 完成 M4.5**
> - 交付：`agent/checkpoint.py`（list/pause/cancel/resume/fork/rollback 5 个纯函数 + `CheckpointSummary` dataclass）；`agent/langfuse_handler.py`（懒单例 `init_langfuse` + `build_callback_handler` + `build_trace_metadata`，缺 key 全返 None）；`build_graph(deps, *, checkpointer=...)` 接受可选 saver，生产 `AsyncPostgresSaver`、测试 `InMemorySaver`；`AgentState` 早已带 paused/run_id，9 个节点开头都已检测 cancelled/paused → `langgraph.types.interrupt`（M4.8 batch A.2 完成迁移）
> - 依赖：新增 `psycopg[binary,pool]>=3.2` 满足 langgraph-checkpoint-postgres 运行时 libpq 需求
> - 测试：unit 16（checkpoint 操作 + langfuse 工厂兜底）+ integration 5（cancel / pause→重启→resume / fork / rollback 一致性 / rollback 超界 wipe）；全部用 InMemorySaver 自包含跑，不要 PG。`make lint` 全绿，全量 148 测试通过
> - 自主决策记录（CLAUDE.md §4.3）：(1) §8 文档 Langfuse 例子停留在 v2 API（`langfuse.callback.CallbackHandler(public_key=...,session_id=...)`），实际 langfuse 4.6.1 把 session/user 移到 RunnableConfig.metadata（`langfuse_session_id` 等），handler 不再吃这些参数 —— 实现按 v4 API 走，文档示例已陈旧（保留注释）；(2) LangGraph v1+ 把 interrupt 改为不抛异常、把 `__interrupt__` 注入返回字典 + snap.next 指向被打断节点，测试断言据此调整；M4.5 临时保留 `raise NodeInterrupt(...)` 兼容；**M4.8 batch A.2 已统一迁移到 `langgraph.types.interrupt({"reason": ...})`，DeprecationWarning 清零**；(3) rollback 用 `adelete_thread` + `aupdate_state` 重落最后一个 checkpoint，符合"不可逆"语义；不做单 checkpoint 精细删除（saver 未暴露公共 API）
> - 留给人审：`[human]` Langfuse trace 验证；fork 时原会话 `status=archived_branch` 的写入由 backend API 层（M5）做，本层只动 LangGraph state 隔离
> - 剩余风险：(1) `psycopg[binary]` ~10MB wheel，CI 缓存命中即可；(2) `langgraph.types.interrupt` 迁移已落地（M4.8 batch A.2），未来 langgraph 主版本若再变签名需要重测；(3) 真实 AsyncPostgresSaver 在 PG 上要先 `await saver.setup()` 建 `langgraph_*` schema，由 M5 backend lifespan 负责

## 15. 完成后下一步

→ `04-backend-api.md` 把 agent 包成 FastAPI 路由，对外暴露 SSE 与 checkpoint API。
