# 建议相似问题（"你想问的是不是…"）功能设计

**日期**：2026-05-20
**范围**：当 agent 回答触发"未找到"短语时，在主回答下方附加可点击的"高可信度协议内相似问题"建议区
**触发**：人重审 `2026-05-19-m7.0-negative-split.md`（B 方案）后改方向 → 主回答统一"未找到"+ UI 附加建议区
**协议**：`CLAUDE.md` §1-§9 + `00-vibe-coding-protocol.md` §4
**关联文档**：
- `2026-05-19-m7.0-negative-split.md`（已作废，作历史背景）
- `2026-05-19-m7.0-drop-tool.md`（评测侧 negative 仍按此单类设计）
- `docs/03-development/03-agent.md` §7 SSE event 表
- `docs/03-development/04-backend-api.md` §4.2 SSE 事件序列
- `docs/03-development/05-frontend.md`（前端链接拦截）
- `docs/03-development/06-evaluation-and-observability.md` §3.4 / §4 / §12（评测侧不动）

---

## 1. 决议（人 2026-05-20 已确认）

| # | 问 | 决议 |
|---|---|---|
| D1 | M7.0 negative 拆 2 类是否继续？ | **作废**，回退到 `drop-tool.md` 的单一 `negative`。本文档负责把"建议问题"功能落到 agent / backend / 前端，与评测解耦 |
| D2 | 建议问题的来源 | **B. spec 段落反生成**：原 query → 向量召回 spec 段落 → LLM 把段落反生成"标准问法" |
| D3 | "高可信度"阈值 | **cosine ≥ 0.75**；召回为 0 不显示建议区；**最多 3 条** |
| D4 | 触发条件 | 所有触发 `must_say_not_found` 短语（中英两套，见 §3.1）的回答**统一**接建议区，**不挑 category** |
| D5 | 超链接交互形态 | **前端方案**：返回 markdown 链接 `[问题文本](cursor-suggest:<urlencoded 问题文本>)`；前端拦截 `cursor-suggest:` scheme → 自动以该问题在当前会话发起新一轮提问 |
| D6 | 评测范围 | **不进评测**。建议区有/无、命中数都不影响 daily/weekly eval 任何指标 |
| D7 | 立项节奏 | 先做评测回退（M7.0 改回单 negative）→ 再做本功能 |

---

## 2. 功能描述（产品语言）

**触发场景**：用户问的问题在 3GPP 协议中找不到（包括纯伪问题、概念混淆、术语误用等所有 agent 判定为"未找到"的情况）。

**响应形态**（在 final SSE event 或其后追加）：

```
3GPP 当前规范未涉及"5G UE 的 MAC 地址格式"。

---

**你想问的是不是？**

- [PDU Session 如何分配 IP 地址？](cursor-suggest:PDU%20Session%20%E5%A6%82%E4%BD%95%E5%88%86%E9%85%8D%20IP%20%E5%9C%B0%E5%9D%80%EF%BC%9F)
- [C-RNTI 是什么？](cursor-suggest:C-RNTI%20%E6%98%AF%E4%BB%80%E4%B9%88%EF%BC%9F)
- [5G NR 的 RNTI 类型有哪些？](cursor-suggest:5G%20NR%20%E7%9A%84%20RNTI%20%E7%B1%BB%E5%9E%8B%E6%9C%89%E5%93%AA%E4%BA%9B%EF%BC%9F)
```

用户点击任意链接 → 前端自动把该问题文本提交为新一轮 user message，复用当前会话上下文。

**降级**：召回 0 / 都低于阈值 / LLM 反生成失败 → 不显示分隔线和建议区，只回"未找到"主回答。

---

## 3. 触发判定

### 3.1 `must_say_not_found` 短语词表（双语）

直接复用 `eval/runner.py` 计划里的同套短语，**单点定义**为 `backend/app/agent/not_found_phrases.py`，供 agent 与 eval runner 共享导入（避免两边漂移）：

```python
NOT_FOUND_PHRASES_EN = frozenset({
    "not found", "not specified", "no such",
    "does not define", "is not defined in", "outside the scope",
})
NOT_FOUND_PHRASES_ZH = frozenset({
    "未找到", "未定义", "规范未规定",
    "不涉及", "不在范围内", "没有相关规定",
})
```

判定函数：

```python
def is_not_found_answer(answer: str, language: str) -> bool:
    """对 agent 最终回答做 substring 扫描。language 来自 classify 节点输出（已有）。"""
    phrases = NOT_FOUND_PHRASES_ZH if language == "zh" else NOT_FOUND_PHRASES_EN
    return any(p in answer.lower() for p in phrases) if language == "en" \
        else any(p in answer for p in phrases)
```

### 3.2 触发时机

在 LangGraph 终态节点 `generate` 之后、`final` 事件构造之前插入 `suggest_questions` 节点（详见 §4）。命中 `is_not_found_answer(...)` → 进入建议生成；否则直接走 final。

---

## 4. 后端实现方案

### 4.1 Agent 节点：`suggest_questions`

新建 `backend/app/agent/nodes/suggest_questions.py`，编排步骤：

1. **检索**：复用现有 `app/retrieval/hybrid.py` 的入口，用**原 user query** 召回 top-N spec 段落（N=10，默认走完整 dense+BM25+RRF；**不**走 rerank 以省时）
2. **阈值过滤**：保留 `score ≥ 0.75` 的段落；为 0 → 直接返回 `[]`
3. **LLM 反生成**：把过滤后的段落（最多取 top-5 段落，去重 spec_id + section_path）拼成 prompt，调 `glm-4.6`（temperature=0），让它输出**最多 3 个**"基于这些段落、用户可能真正想问的问题"
4. **后处理**：
   - 去重（substring 包含视为重复）
   - 长度限制（每条 ≤ 80 字 / 60 单词）
   - 失败容忍：LLM 报错 / 输出非 JSON → log warning + 返回 `[]`，不阻塞主流程
5. **返回**：`list[SuggestedQuestion]`，结构见 §4.3

**Prompt 模板**（中英双语，按 `state.language` 切；prompt 全文由 agent 起草第一版交人 review）：

```
你是 3GPP 协议问答助手的辅助模块。用户刚问了一个在协议中找不到答案的问题。
下面是基于用户原问题召回的、与之最接近的协议段落（按相关度排序）。

【用户原问题】
{user_query}

【召回段落】
{numbered_paragraphs}

任务：基于上述段落，推断用户**可能真正想问**的问题，最多输出 3 个。
要求：
1. 每个问题必须是该段落能直接回答的
2. 用中文/英文（同用户语言）
3. 简洁，不超过 80 字 / 60 单词
4. 不输出解释，只输出 JSON 数组
5. 如果段落与用户问题都对不上，输出 []

输出格式（严格 JSON）：
["问题1", "问题2", "问题3"]
```

### 4.2 LangGraph 接线

修改 `backend/app/agent/graph.py`：

- 在 `generate` 之后加 `suggest_questions` 节点
- `suggest_questions` 节点先调 `is_not_found_answer(state.answer, state.language)`：
  - `False` → 直接 passthrough（state 不动）
  - `True` → 调 §4.1 逻辑，把结果写入 `state.suggested_questions`
- 边：`generate → suggest_questions → END`（之前是 `generate → END`）

**性能注意**：suggest_questions 走召回 + 1 次 LLM 调用，估额外延迟 1-2s。仅在"未找到"时触发，对正常问答**零开销**（passthrough 直接出）。

### 4.3 State / SSE event 新增

**AgentState 加字段**（`backend/app/agent/state.py`）：

```python
class SuggestedQuestion(TypedDict):
    text: str           # 问题文本
    source_spec: str    # 来源 spec_id（debug 用，前端可不显示）
    source_section: str # 来源 section_path（debug 用）

class AgentState(TypedDict):
    ...
    suggested_questions: list[SuggestedQuestion]  # 默认 []
```

**SSE event 新增**（`docs/03-development/03-agent.md §7` + `04-backend-api.md §4.2` 同步）：

```
event: suggested_questions
data: {"questions":[
  {"text":"PDU Session 如何分配 IP 地址？","source_spec":"23.501","source_section":"5.6.4"},
  ...
]}
```

时序：在 `final` event **之后**、`end` event **之前**单独发一条；前端按需渲染。

**也保留 final 内联**（兼容方案）：`final.data` 里 answer 字段**已经**带 markdown 链接形式，前端不依赖 `suggested_questions` event 也能展示（直接渲染 markdown 即可）。`suggested_questions` event 是结构化备份，供前端做点击行为劫持时拿原始文本。

> **取舍**：双通道（answer 内联 + 单独 event）冗余但稳，且让 agent 不需要做 markdown 拼接、由前端统一控制 UI 样式（D5 决策的"前端方案"本质）。

### 4.4 答案文本拼接

`generate` 节点输出的 `answer` 本身**不**带建议区文本。建议区由 `suggest_questions` 节点在 passthrough 后追加：

```python
# in suggest_questions node, after generating questions list
if not questions:
    return state  # 不动 answer
suggestions_md = render_suggestions_markdown(questions, language=state.language)
state.answer = f"{state.answer}\n\n---\n\n{suggestions_md}"
state.suggested_questions = questions
return state
```

`render_suggestions_markdown` 实现（`backend/app/agent/nodes/suggest_questions.py` 内）：

```python
from urllib.parse import quote

HEADER_ZH = "**你想问的是不是？**\n\n"
HEADER_EN = "**Did you mean to ask?**\n\n"

def render_suggestions_markdown(questions: list[SuggestedQuestion], language: str) -> str:
    header = HEADER_ZH if language == "zh" else HEADER_EN
    lines = [f"- [{q['text']}](cursor-suggest:{quote(q['text'])})" for q in questions]
    return header + "\n".join(lines)
```

---

## 5. 前端实现方案

> 详细 UI/UX 细节归 `docs/03-development/05-frontend.md`；本文档只列必要约束。

### 5.1 链接拦截

前端在渲染 assistant message 的 markdown 时（react-markdown 或同类），**重写 `<a>` 组件**：

```tsx
function SuggestionLink({ href, children }: { href: string; children: ReactNode }) {
  if (href.startsWith('cursor-suggest:')) {
    const question = decodeURIComponent(href.slice('cursor-suggest:'.length));
    return (
      <button
        className="suggestion-link"
        onClick={() => submitMessage(question)}  // 复用现有发消息 hook
      >
        {children}
      </button>
    );
  }
  return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
}
```

`submitMessage(question)` 复用现有 chat 输入框的提交逻辑（同会话 sid，新一轮 SSE）。

### 5.2 样式

建议区与主回答之间已有 markdown `---` 分隔；列表项渲染成可点击 chip 风格（区别于普通 markdown 链接），避免用户误以为是外链。

### 5.3 降级

如果前端解析失败（cursor-suggest scheme 未注册 / 旧版本前端），保留为普通 link 形态，用户至少能看到问题文本，**不阻塞使用**。

---

## 6. 评测影响（明确"不动"清单）

| 项 | 是否改动 | 说明 |
|---|---|---|
| `eval/validators/golden.py` 的 `CATEGORY_ENUM` | **不动** | 保持 `drop-tool.md` 的单一 `negative`，**不**加 `negative_clarify` / `negative_no_steelman` |
| `eval/validators/golden.py` 的 `expected_clarification_topic` 字段 | **不加** | 已作废 M7.0 拆分文档里的字段，本次彻底删除规划 |
| `eval/judges/clarification.py` | **不建** | LLM-as-judge 模块取消 |
| `eval/runner.py` 的 `clarification_judge_passed` | **不加** | EvalResult 不新增 clarify 相关字段 |
| `must_say_not_found_passed` 双语判定 | **保留**（按 `2026-05-19-m7.0-negative-split.md §4.1` 描述的词表，独立挪到 `not_found_phrases.py` 单点） | 这是 negative 单类的唯一断言；建议区是否出现不影响该指标 |
| `forbidden_violations` 扫描 | **保留** | 建议区文本也参与 forbidden 扫（防止反生成出错误概念）；这是已有规则的自然延伸 |
| `eval/golden/v1.yaml` 旧 3 题 negative | **不迁移子类** | 保持 `category: negative` |
| `eval/golden/_template.yaml` | **小改** | 在 negative 示例 notes 里加一句说明："agent 主回答应触发'未找到'短语；下方可能附建议区，建议区不在评测范围"。**不**改 schema / 不加字段 |
| Ragas / native MCQ / Langfuse Dataset | **不动** | M7.2 / M7.3 计划不受影响 |

**关键**：本功能与评测体系**完全解耦**。daily/weekly eval 跑出的指标在引入本功能前后**应该完全一致**（除非建议区文本意外触发 `forbidden`，那是 bug 不是设计）。

---

## 7. 文件清单

| 文件 | 改动类型 | 关键改动 |
|---|---|---|
| `backend/app/agent/not_found_phrases.py` | **new** | §3.1 双语短语词表 + `is_not_found_answer()` 函数；供 agent 与 eval 共享 |
| `backend/app/agent/nodes/suggest_questions.py` | **new** | §4.1 节点实现（检索 + 阈值 + LLM 反生成 + markdown 拼接） |
| `backend/app/agent/graph.py` | edit | §4.2 接 `generate → suggest_questions → END` |
| `backend/app/agent/state.py` | edit | §4.3 加 `suggested_questions: list[SuggestedQuestion]` |
| `backend/app/api/v1/chat.py`（或对应 SSE 流处理） | edit | §4.3 在 `final` 后、`end` 前发 `suggested_questions` event |
| `backend/app/agent/prompts/suggest_questions.{zh,en}.txt` | **new** | §4.1 反生成 prompt 模板（中英各一份；agent 起草，人 review） |
| `backend/tests/unit/agent/test_suggest_questions.py` | **new** | mock retrieval + mock LLM；测：触发判定 / 阈值过滤 / LLM 返回非 JSON 容忍 / markdown 渲染 / 0 召回 passthrough |
| `backend/tests/unit/agent/test_not_found_phrases.py` | **new** | 中英短语命中边界（大小写 / 半角全角 / 标点） |
| `backend/tests/integration/agent/test_graph_suggest_questions.py` | **new** | 端到端：fake state，负样本 query → 建议区出现；正样本 query → 不触发；fail-safe（LLM 挂）不阻塞 |
| `backend/tests/integration/api/test_chat_sse.py` | edit | 加一条断言：负样本 query 的 SSE 序列含 `suggested_questions` event；正样本不含 |
| `frontend/src/components/SuggestionLink.tsx` | **new** | §5.1 链接拦截组件 |
| `frontend/src/components/MessageRenderer.tsx`（或现有 markdown 渲染入口） | edit | 注入 `SuggestionLink` 作为 `a` 重写 |
| `frontend/src/__tests__/SuggestionLink.test.tsx` | **new** | 单测：cursor-suggest scheme 触发回调；其他 scheme 走默认外链 |
| `docs/03-development/03-agent.md §7` | edit | SSE event 表加 `suggested_questions`；节点图加 `suggest_questions` |
| `docs/03-development/04-backend-api.md §4.2` | edit | SSE 事件序列加 `suggested_questions` 示例 |
| `docs/03-development/05-frontend.md` | edit | 加"建议问题链接拦截" UX 章节 |
| `docs/03-development/06-evaluation-and-observability.md` | edit | §3.4 / §3.5 / §4 / §12 把之前 M7.0 拆 negative 的 TODO **撤回**；明确建议区不进评测 |
| `docs/04-handoff/2026-05-19-m7.0-negative-split.md` | edit（已完成） | 顶部加 SUPERSEDED 标记 |

---

## 8. 实施顺序

按"先评测回退，再功能立项"的人决策（D7）：

### 8.1 Phase A — 评测回退（小 PR，半天）

> 把 M7.0 拆 negative 的所有规划"擦掉"，回到 `drop-tool.md` 的 6 类基线。

1. `docs/03-development/06-evaluation-and-observability.md`：把当晚加的 `must_say_not_found_passed` 双语 TODO 改成"对所有 `negative` 题生效"（不再分子类）
2. `eval/golden/_template.yaml`：negative 示例 notes 补充"主回答 = 未找到；下方可能附建议区，建议区不在评测范围"
3. 单测：确认 `test_validators_golden.py` 没有任何 `negative_clarify` / `negative_no_steelman` 残留（drop-tool PR 已是 6 类，理论上没残留）
4. 跑：`uv run --project eval ruff check && uv run --project eval pytest tests/unit/ -q`

### 8.2 Phase B — 后端节点 + SSE event（1-2 天）

5. `backend/app/agent/not_found_phrases.py` + 单测
6. `backend/app/agent/nodes/suggest_questions.py` + prompt 模板 + 单测（mock retrieval / mock LLM）
7. `backend/app/agent/graph.py` + `state.py` 接线
8. `backend/app/api/v1/chat.py` SSE event 追加
9. 集成测：fake LangGraph fixture 灌负样本 / 正样本两路；断言 SSE 序列
10. 跑：`cd backend && uv run pytest -m unit -q && uv run pytest -m integration -q`

### 8.3 Phase C — 前端拦截（半天）

11. `SuggestionLink.tsx` + 单测
12. `MessageRenderer` 注入
13. 手动 smoke：起 backend + frontend，问一道负样本，看建议区可点击，点击后新一轮 SSE 跑通

### 8.4 Phase D — 文档同步 + handoff（1 小时）

14. `03-agent.md` / `04-backend-api.md` / `05-frontend.md` / `06-eval...md` 全部按 §7 清单同步
15. 出 `docs/04-handoff/yyyy-mm-dd-suggested-questions-complete.md` 完成报告（CLAUDE.md §6.4）

每段都按 CLAUDE.md §4.1 "新代码必带测试" + §6 "plan → implement → self-verify"。

---

## 9. 不动的部分（写明免得 agent 顺手改）

- `runner_retrieval.py` / `runner.py`（M7.1）：不动；建议区不进评测
- `validators/golden.py` 的 schema：保持 `drop-tool.md` 6 类
- TeleQnA pull / filter / transform：不动
- 第一档 / 第二档评测阈值：不动
- Ragas 4 metric / native MCQ / Langfuse Dataset 计划：不动
- `must_say_not_found` 词表本身：内容不动，只是从 eval runner 内联挪到 `backend/app/agent/not_found_phrases.py` 单点定义；eval runner 改成 `from app.agent.not_found_phrases import ...` 或 vendor 一份（看打包边界）
- 取消接口 / checkpoint 接口 / 鉴权：不动

---

## 10. §5 触发条件自检

| §5 条 | 是否命中 | 说明 |
|---|---|---|
| §5.1 全局决策表 | ❌ | 不动 `00-overview.md §2` |
| §5.2 花钱大批量 | ⚠️ 间接 | 每次"未找到"多 1 次 glm-4.6 调用（~ 800 tokens prompt + 100 tokens output ≈ ¥0.001）。daily eval 负样本约 20 题 → 多 ¥0.02/天，远低于阈值 |
| §5.3 安全 | ❌ | 无鉴权 / CORS / 密钥改动 |
| §5.4 删数据 | ❌ | 不动 DB / Qdrant |
| §5.5 改产品决策 | ✅ **本文档即上报凭据，人已 approve D1-D7** | 同时撤销 `2026-05-19-m7.0-negative-split.md` 的 B 方案决议 |
| §5.6 评测门槛降级 | ❌ | 不降阈值；建议区与评测解耦 |
| §5.7 依赖大改动 | ❌ | 无新依赖；retrieval / LLM client 都复用现有 |
| §5.8 多方案权衡不清 | ❌ | D1-D7 已逐条 approve |
| §5.9 连续两次失败 | — | 触发再上报 |
| §5.10 运行成本异常 | ❌ | 见 §5.2 估算 |

---

## 11. 风险与边界

| 风险 | 缓解 |
|---|---|
| LLM 反生成出错误概念（hallucination） | 建议区文本进 `forbidden` 扫；§4.1 LLM 失败容忍直接降级到空建议区；prompt 强约束"只能基于召回段落" |
| retrieval 召回 spec 段落与原 query 相关性低 | 已有 §3 阈值 0.75 + 召回 0 不显示双重保险；写测试覆盖低分场景 |
| 建议区 markdown 链接被前端误识别为外链导致用户离开页面 | `cursor-suggest:` scheme 在前端**强制**拦截，且 PR 中加单测；旧版本前端 fallback 成普通文本 |
| URL encode 边界（中文、emoji、引号） | 用 `urllib.parse.quote(text)` 而非 `quote_plus`；前端用 `decodeURIComponent`；加单测覆盖中文/特殊符号 |
| suggest_questions 节点失败拖累主回答 | 节点用 try/except 包住，失败 → 返回原 state（answer 不动），不阻塞 final event |
| 用户连点多个建议链接 → 多轮并发 SSE | 前端按现有"上一轮未结束禁止发新消息"规则约束（已存在）；本功能不引入新并发风险 |
| 中英语言判定错误 → 双语短语都不命中 → 建议区不出 | `language` 字段已在 classify 节点产出且 M4 集成测覆盖；本功能只是消费，不引入新判定 |

---

## 12. 待人确认的次要项（明早 agent 看到这条 → 进 §6.1 plan 阶段先问）

1. **Prompt 模板正文**（§4.1）：本文档没列正文，由 agent 起草第一版交人 review；要求中英双语、温度 0、JSON 输出、强约束"只能基于召回段落"
2. **suggest_questions 节点是否要让用户在 UI 上关掉**？（如某些"未找到"用户其实就是想确认协议没规定，不希望被推荐）→ 默认**始终开**，看用户反馈再加 toggle
3. **建议问题的 markdown 列表样式**：项目符号 `-` vs 编号 `1.` vs 自定义 chip 样式？**默认 `-`**（最简单，前端 chip 化由 CSS 完成）
4. **是否在主回答前缀加一句"以下是可能的相关问题"**？**默认不加**，让 markdown 分隔线 + 加粗 header `**你想问的是不是？**` 自然表达
5. **召回是否走 rerank**？**默认不走**（省 1 次 voyage rerank 调用，retrieval 已能给出可用分数）；如发现质量差再加
6. **本功能要不要在 Langfuse trace 里单独标 node**？**默认加**（不增成本，方便后续分析）

---

**协议**：按 `CLAUDE.md §6.1` plan 阶段，进入 implement 前若有上述任一条人想改 → 先在本文档下方追加决议，再动代码。
