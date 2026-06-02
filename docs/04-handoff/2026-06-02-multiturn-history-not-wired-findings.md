# 2026-06-02 · 多轮对话历史未被 agent 消费（实现缺口诊断 + 接通计划）

> ✅ **2026-06-02 已接通**（A 层查询上下文化 + B 层生成带历史）。§4 四个人审问题已定：
> (1) 做真多轮；(2) 历史**不可引用**、不可作为事实来源（grounding 口径不变）；
> (3) generate 回答**原始**问题，历史作只读上下文；(4) 本次范围 A+B + 单元/集成测，
> 多轮 eval 子集留下一个 PR。实现要点：
> - 承载字段换成普通字段（覆盖语义）`AgentState.raw_history`（路由加载的未压缩 prior
>   历史）+ `history`（图内压缩产物），**不用** `messages`——生产挂 `AsyncPostgresSaver`
>   时 `add_messages` reducer 会把每轮重载的全量历史跨 checkpoint 累积导致重复膨胀
>   （本文档原计划未发现这一点，是接通时的关键修正）。
> - A：新增 `nodes/contextualize.py` + `prompts/contextualize.md`，`build_graph` 条件
>   入边 `_entry_router`（`raw_history` 非空才走 `compact_history → contextualize`，
>   首轮直连 classify）；classify/rewrite/hyde/self_rag 经 `AgentState.effective_query`
>   消费消解后的查询。
> - B：`generate_qa.md` v7 加只读历史段 + rule 7（历史不可引用）；`generate.py` 传
>   `state.history`，仍答原始 `user_input`。
> - **compaction 改由 `build_graph` deps 注入**（§4 §3 计划外补强）：新增
>   `nodes/compact_history.py`，经 `deps.llm` + `deps.redis` 跑 `compact_history()`，
>   `chat.py` 不再调它。附带修掉生产隐性 bug：旧实现 compaction 依赖
>   `app.state.litellm_client`（lifespan 从未接线 → 恒 None → summary 路径在生产从未
>   触发）；改 `deps.llm` 后 summary 才真正生效。
> - 文档同步：`03-agent.md §2 / §6.1` 已更新。下方原"诊断 + 计划"保留作历史记录。

> 起因：调查「fork 从精确位置分叉」工作量时，顺藤摸到一个更底层的问题——
> 会话历史被加载、压缩、塞进 LangGraph `state.messages`，但**没有任何节点真正
> 读它**，generate 的 prompt 里也没有历史插槽。结果：多轮追问/指代类问题实际被
> 当成单轮处理，且长会话的 history summary LLM 调用是纯浪费。
>
> 本文档把诊断证据、影响面、接通计划、待人决策记下来。**本次只调查归档，不改代码。**
>
> 锚：
> - 设计意图：[`docs/03-development/03-agent.md §2 / §6.1`](../03-development/03-agent.md)
> - 历史加载/压缩：`backend/app/api/v1/chat.py` `_load_history` / `compact_history` 调用点
> - 压缩器：`backend/app/agent/utils/history_compactor.py`
> - 生成 prompt：`backend/app/agent/prompts/generate_qa.md`
> - state 定义：`backend/app/agent/state.py` `AgentState.messages`

## 0. TL;DR

| 项 | 结论 |
|---|---|
| 设计意图 | **多轮**：`03-agent.md §6.1` 明写「使 LangGraph 节点对 history 字段直接消费」 |
| 实际现状 | **断在最后一公里**：history 进了 `state.messages`，但**没有任何节点读它** |
| 功能影响 | 追问/指代类问题（「它的默认值？」「那 5G 呢？」）被当独立问题：检索拿原文去查（无指代消解）→ 错 chunk；生成看不到上文 → 答非所问 |
| 成本影响 | `message_count > 8` 时 `compact_history` 调 `mimo-v2.5` 生成 summary，产物进了没人读的 `state.messages` → 这笔 LLM 开销零产出（Redis 24h 缓存兜底，但首次真花钱） |
| 性质 | **实现缺口**，非有意单轮设计 |
| 修复定级 | M~L，风险中高（核心：历史进 prompt 不能破坏 generate 的严格 grounding 护栏） |

## 1. 诊断证据

### 1.1 历史被加载并压缩进 state（plumbing 是通的）

`chat.py` 每轮从 PG 全量重建历史，经 `compact_history` 注入 `initial_state.messages`：

- `_load_history(db, sid, exclude_id=assistant_msg_id)` → 按 `created_at asc` 取该会话全部 message
- `compact_history(...)` → `message_count <= 8` 取最近 N=6 条原文；`> 8` 把更早的 batch 给 `mimo-v2.5` 做单段 summary，拼成 `[SystemMessage(summary), 最近 N 条]`
- `_build_initial_state(..., messages=lc_history, ...)` → 塞进 `AgentState.messages`

### 1.2 但没有任何节点读 `state.messages`（消费断了）

- 全 `backend/app/agent` 目录搜 `.messages`，只命中 `state.py`（定义）与 `history_compactor.py`（自己），**没有任何 node 引用**。
- 所有节点构 prompt 只用 `state.user_input`（及其派生 `rewritten_queries`）：classify / rewrite / hyde / multi_query / retrieve / rerank / generate / self_rag 全部如此。
- generate 的 prompt 模板 `generate_qa.md` **没有历史插槽**，只有 `chunks` + `user_input` + `user_language`：

  ```
  Retrieved chunks (top {{ chunks|length }}):
  {% for c in chunks %} ... {% endfor %}
  User question ({{ user_language }}):
  {{ user_input }}
  ```

### 1.3 设计意图本是多轮（所以这是缺口不是 feature）

`03-agent.md §6.1` 收尾一句：

> 实现位置（约定）：`history_compactor.py`，由 `build_graph` 的 deps 注入，**使 LangGraph 节点对 history 字段直接消费**。

`state.py` / §2 也写 `messages` 用 `add_messages` reducer「多轮自然累积」。即：设计要多轮，plumbing 也铺了，唯独「节点消费 history」这步从没落地。

> 附带偏差：§6.1 说 compact_history「由 `build_graph` 的 deps 注入」，实际是在 `chat.py`
> 路由入口调用后塞进 `initial_state`，没走 deps。属次要实现偏离，本文档一并记录。

## 2. 影响面细化

### 2.1 功能：多轮追问实际是单轮

两个子环节都缺历史，缺一不可：

1. **检索侧**：追问「它的默认值是多少？」时，`retrieve` 用 `user_input` 原文（或 `rewrite` 改写，但 rewrite 也不读历史）去检索。「它」无法被消解成具体 IE/参数 → 检索到不相关 chunk。
2. **生成侧**：即便检索侥幸命中，generate prompt 看不到上文 → 无法理解这是对上一轮的延续。

用户感知：界面是多轮对话，但每问被独立回答；指代、省略、「继续」「展开第 2 点」这类全部失效。

### 2.2 成本：长会话 summary 是无效开销

- 触发条件：单会话 `message_count > 8`。
- 行为：调 `mimo-v2.5`（thinking=disabled，max_tokens=800）生成 summary。
- 去向：summary → `SystemMessage` → `state.messages` → **无人消费**。
- 缓解：Redis `tgpp:cache:history_summary:{sid}:{last_id}` TTL 24h，相同 (sid, last_id) 不重复调；但每个新回合 `last_id` 变 → 长会话每问都可能触发一次新 summary。
- 量级：与"长会话活跃度"成正比；不是大头，但属于"花了钱没产出"，接通后立即转为有效成本。

## 3. 接通计划（建议立项，分两层）

> 真正修复不是「prompt 加个历史段」那么简单，要同时解决检索侧与生成侧，且不破坏 grounding。

| 层 | 改动要点 | 量 | 风险 |
|---|---|---|---|
| **A. 查询上下文化**（关键，先做） | 让追问在检索前被改写成「自包含查询」：`rewrite` 节点读 `state.messages`，把指代/省略补全（"它的默认值" → "PUCCH-Config 的 X 字段默认值"）。prompt 增历史段 + `render()` 传历史 | M | 中：改写质量直接决定检索质量；过度改写会引入幻觉关键词 |
| **B. 生成带历史** | `generate_qa.md` 增「对话历史（仅供理解指代，不得作为事实/引用来源）」段；`generate.py` `render()` 传 `state.messages`（或其精简文本） | S | **中高**：generate 是严格 grounding（只能用 chunks 答 + `[N]` 引用）。历史进 prompt 必须明确「历史不可引用、不可作为事实来源」，否则破坏护栏，self_rag 也兜不住 |
| **C. 回归/评测** | 新增多轮 eval 样本：指代消解正确性 + grounding 不被历史污染（faithfulness 不掉）；§6.1 文档同步改成「已接通」 | M | 需要构造多轮测试集 |

### 3.1 实现顺序建议

1. 先 A（没有自包含查询，B 的历史也救不了检索 miss）。
2. 再 B，重点打磨 grounding 护栏文案 + 用 spike 验证（仿 `spike_citation_index.py` 思路）历史不污染引用。
3. 最后 C，跑多轮 eval 子集，确认 faithfulness / context_recall 不退化。

### 3.2 可选的"最小接通"（不推荐作为终态）

只做 B（generate 加历史）不做 A：能让生成读懂上文措辞，但检索仍 miss 指代类追问 → 半通。可作为快速验证 grounding 风险的 spike，但不能当完整交付。

## 4. 待人决策（开工前需确认）

1. **要不要做多轮**：这是产品决策。当前是"伪多轮"，要么补成真多轮（走本计划），要么显式降级为"单轮问答 + 不加载历史"（省掉 compact_history 的无效成本）。两条路都行，但要明确选一条。
2. **grounding 口径**：历史进 generate prompt 后，"历史里的事实"能不能被当答案的一部分？建议**不能**（历史仅用于理解指代，所有事实仍须来自 chunks 并 `[N]` 引用），但需人拍板，因为这影响 self_rag / faithfulness 评测口径（属 CLAUDE.md §5.6 评测门槛相关）。
3. **成本预算**：A 层会让 rewrite 多吃 history → 每轮 token 上升；接通后 summary 成本转为有效。需确认在预算内。

## 5. 与「精确位置分叉」的关系

上一轮分析「fork 从精确位置分叉」时定级"低价值"，根因正是本问题：**agent 不看历史，fork 到哪个 checkpoint 都不改变答案**。正确顺序是先定多轮（本计划），再回头评估分叉精度——否则精确 checkpoint 没有落地价值。

## 6. 本次结论

- 仅调查 + 归档，**未改任何代码**。
- 同步在 `03-agent.md §6.1` 加了一条指向本文档的现状偏差说明（那句"节点直接消费 history"与现状不符）。
- 下一步取决于 §4.1 的产品决策：补成真多轮（走 §3 计划）还是显式降级为单轮。
