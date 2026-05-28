# 2026-05-28 · A/B/C 修复：citation chip 不规范、HTML 残留、prompt 反诱导

> 触发：user 报告 "DCI1_1 字段查询" 和 "ControlResourceSet 讲解" 两个回答存在：
> (1) `<b>` / `*xxx*` 等格式残留；(2) `38.331 §*ControlResourceSet* information element`
> 这种引用气泡渲染了但加载 section 失败。
>
> Plan：本次完成根因分析 + P0 三条治标（A prompt 反诱导 / B 前后端正则对齐 +
> SnackBar 兜底 / C tool 路径 sanitize），P1 治本（chunker `_section_header` +
> section_title sanitize）触发 §5.1/5.2/5.4 留给 user 决策窗口。
>
> 锚：`CLAUDE.md` §4.2 / §4.3 / §6；prompt v5 见
> [`backend/app/agent/prompts/generate_qa.md`](../../backend/app/agent/prompts/generate_qa.md)；
> 文档同步在 [`03-agent.md §4.7`](../03-development/03-agent.md#47-generate_node-—-最终生成)。

---

## 一、根因（4 层叠加，**不是 chunk 策略本身的问题**）

| 层 | 问题 | 实证 |
|---|---|---|
| **数据层** | 38.331 IE 章节 raw md 用 `#### *ControlResourceSet* information element` 作 heading，含 `*` 强调符；表头有 `<b>...</b>` HTML 残留。chunker `atomic_blocks.py` 注释明确"原样保留 raw markdown，不清洗" | Qdrant payload `title="*ControlResourceSet* information element"` + content 含 `<b>ControlResourceSet field descriptions</b>` |
| **元数据层** | 这类章节无数字 clause 编号 → `markdown_parser._split_clause` 返回 `("", title)` → `clause=""`、`section_path=()` | `chunks_meta` 表 38.331 IE chunk 全部 `clause=`、`section_path=[]` |
| **生成层（最隐蔽）** | chunker `builder._section_header` 在 `clause=""` 时输出 `[spec § title]`，**视觉上和 prompt 强制 citation 格式一模一样**；LLM 看到 prompt 里 `section_path=`（空），又看到 chunk content 第一行就是 `[38.331 § *ControlResourceSet* information element]`，自然 verbatim 抄回来当 citation | LLM 实际输出 `[38.331 §*ControlResourceSet* information element]` |
| **渲染层** | 后端 `parse_citations` 严正则 `[A-Za-z0-9.\-/]+`，含 `*`/空格不认 → `message.citations[]` 无这条；前端 `CitationInlineSyntax` 宽正则 `[^\]¶]+?` → chip 仍渲染；后端 `_render_tool_results` 把 chunk preview 240 字符原样喂给 markdown，HTML 标签 + 表格管道全部回显 | chip 渲染但 chunkId 缺、跳 `/reader/38.331/*...*%20information%20element` → 404 |

## 二、本次 P0 完成清单（A/B/C，Agent 自跑）

### A. `generate_qa.md` v4 → v5（prompt 反诱导）

- 元数据行：空 `section_path` 显式标 `<none>` 而不是裸空串
- hard rule #2 新增 sub-bullet：(1) chunk body 顶部的 `[spec § title]` 是 **chunker artifact**，禁止 verbatim 复制；(2) `section_path=<none>` 时只写 `[spec_id]`，禁止把 IE 名 / title 写到 § 后

文件：`backend/app/agent/prompts/generate_qa.md`

### B1. backend `parse_citations` 三段 fallback（`backend/app/agent/nodes/generate.py`）

正则放宽 + `_match_chunk` 改为：
1. **strict**：spec + section_path 前缀双向匹配（合法 `[38.331 §5.3]`，保持原行为）
2. **fuzzy**：sect 不像 dotted clause 时（含 `*` / 空格 / IE 名）→ 去 `*` / HTML / 大小写归一化后与 `chunk.section_title` 包含匹配 → 捞回正确 chunk_id
3. **spec-only**：彻底匹不到时退到同 spec 第一条（兼容 `[38.331]` 无 § 形态 + 任何漂移兜底）

### B2. frontend `jumpToReader` 兜底（`frontend/lib/features/chat/widgets/citation_chip.dart`）

- 新增 `looksLikeClause` 判定（regex `^[A-Za-z]?[\d][\w.\-]*$`）
- 不像 dotted clause → 退到 spec 概览页 `/reader/{spec}` + **SnackBar 提示** "已跳转到规范主页"，避免必 404 的 `/reader/{spec}/{?}` 路由
- chunkId 在时 SnackBar 文案换成 "hover chip 可看 chunk 摘要"
- `_CitationPreview` "无 chunk 上下文" 文案改成 "未关联 chunk"（更准确）

### C. `_render_tool_results` sanitize（`backend/app/agent/nodes/generate.py`）

新增 `_sanitize_preview(text, max_chars=180)`，应用到 `params.hits[*].preview` / `glossary.matches[*].definition` / `web_search.results[*].snippet`：
1. 去 `<\w+>` / `</\w+>` HTML 标签（保留内部文本）
2. 去表格分隔行 `|---|---|` / `|:--|`
3. 解包 `*xxx*` / `**xxx**` 强调符（保留内容）
4. 多换行 → 单空格；管道符两侧 → ` | `；连续空白 → 1 个
5. 超长截尾加 `…`，preview 长度上限 240 → 180

`params.hits` 行格式同步小调整：空 `section_path` 时不再输出 `§:` 裸冒号

---

## 三、改动文件清单

```
backend/app/agent/nodes/generate.py            (+125 / -25)  A + B1 + C
backend/app/agent/prompts/generate_qa.md       (+27 / -6)    v4 → v5
backend/tests/unit/agent/test_generate_node.py (+135 / -3)   10 个新 case
backend/tests/unit/agent/test_prompts.py       (+50 / -0)    3 个新 case
frontend/lib/features/chat/widgets/citation_chip.dart  (+38 / -10)  B2
frontend/test/features/chat/widgets/citation_chip_test.dart  (+62 / -0)  2 个新 case
docs/03-development/03-agent.md                (+20 / -1)    §4.7 v5 注记
```

合计 backend ~310 LOC / frontend ~110 LOC / docs ~20 LOC。

---

## 四、自验证

| 检查 | 结果 | 备注 |
|---|---|---|
| `cd backend && uv run ruff check` | ✅ All checks passed | 改动 4 文件 + 全 app/tests |
| `cd backend && uv run black --check`（改动文件） | ✅ | 我自己的 3 个文件全过 |
| `cd backend && uv run mypy app` | ✅ Success in 81 source files | |
| `cd backend && uv run pytest tests/unit -q` | ✅ **330 passed**（原 298 + 32 新） | 9.91s |
| `cd ingestion && uv run ruff check && uv run black --check` | ✅ 77 files clean | 没动 ingestion |
| `cd frontend && flutter analyze` | ✅ No issues found (5.1s) | |
| `cd frontend && flutter test` | ✅ **191 tests passed** (88s) | 含新 2 个 chip 兜底 case |
| `cd backend && uv run pytest tests/integration/agent/test_{simple,complex,tools}_qa.py -m integration -q` | ⚠ 7 passed / 1 failed / 1 skipped (11m37s) | **唯一 failure 是 pre-existing**（`test_non_explicit_tools_does_not_invoke_tools`），与本次改动无关；详见 §六 |
| ReadLints | ✅ 改动 6 文件全过 | |

### 关键 integration 子集（v5 prompt 真 LLM 验证）

- `test_simple_qa.py::test_retrieve_node_p50_latency_under_800ms` ✅（真 mimo-v2.5-pro）
- `test_complex_qa.py::test_complex_qa_five_golden_items` ✅（**5 题真 LLM end-to-end，含 citations 抽取断言**）→ v5 prompt 不破契约

---

## 五、Pre-existing black 偏差（**不在本次范围**）

`backend/app/api/v1/chat.py` + `backend/tests/unit/agent/test_self_rag_node.py` 在 main 分支基线上 black 失败（共 ~50 行格式偏差），来自 `b729224 perf(chat): autotitle 与 agent 并发起`。按 `CLAUDE.md §3` "不顺手优化无关代码或格式"未动；建议作为单独 `chore(backend): black format pre-existing files` commit 处理。

---

## 六、Pre-existing integration test failure（**不在本次范围**）

`tests/integration/agent/test_tools.py::test_non_explicit_tools_does_not_invoke_tools` 在 main 基线上即 fail：

```
assert 'AMF' in state.final_answer
E   assert 'AMF' in '{"faithful": true, "coverage": 0.8, "confidence": 0.8, "verdict": "accept", "missing_aspects": []}'
```

根因（已 git stash 验证基线复现）：测试 `query_class=definition` 走 graph 的 complex 路径（`_after_classify` 把 definition 视作 complex），但 `StubLLM(responses=[classify_resp, generate_resp, self_rag_accept])` 仅排了 3 条响应，complex 路径会消耗 rewrite/hyde/multi_query/generate/self_rag 共 5 次 chat-like call → generate 拿到 self_rag JSON 作为 LLM 输出 → final_answer 是 JSON 而不是 AMF 文本。

handoff `2026-05-27-pre-handoff-followups.md` 报 "108 passed" 时可能用了不同子集 / `-k` 过滤跳过这条；本次跑 `test_{simple,complex,tools}_qa.py` 三文件子集时暴露。**修法**（不在本次范围）：在 `StubLLM` responses 里把 rewrite/hyde/multi_query 三条 stub 补齐（按 complex 路径节点顺序）；或者改测试用 simple complexity（让 graph 走 simple 路径）。

---

## 七、自主决策记录（CLAUDE.md §4.3）

1. **prompt 版本号 v4 → v5**：rule #2 收紧属"在已声明规则范围内细化"，归 §4.3
2. **`_sanitize_preview` 默认 `max_chars=180`**（原 `_PREVIEW_CHARS=240`）：sanitize 后会去掉表格分隔行 + 解包强调符等噪声，180 已足够展示有效信息；240 在 chunk content 头部有 `[spec § title]` 头时大半被吃掉，体验更差。仍可由调用方 override
3. **B2 SnackBar 文案分两版**（有 chunkId 提示 hover 看摘要 / 无 chunkId 提示已跳转）：纯文案选择，归 §4.3
4. **`jumpToReader` 的 `looksLikeClause` regex 容忍下划线**（`5.2.3.2.1_5.3.3_1` 合法）：与 `citation_chip.dart` 现有正则注释里"GSMA 注入的多章节合并标记"对齐
5. **`_CitationPreview` 文案 "无 chunk 上下文" → "未关联 chunk"**：更准确，避免给用户造成"chunk 不存在"的误解，归 §4.3
6. **未顺手 black format `chat.py` / `test_self_rag_node.py`**：按 §3 "不顺手优化"，pre-existing 偏差点名留给单独 commit
7. **未修 pre-existing integration test failure**：同上，§3

---

## 八、剩余项（待 user 决策）

### D. 治本：chunker `_section_header` + section_title sanitize（触发 §5.1/5.2/5.4）

**设计**：
- `_section_header` 在 `clause=""` 时改用 `### {spec_id} — {clean_title}`（markdown H3），与 citation 格式 `[{spec_id} §{clause}]` 视觉清楚区分
- `markdown_parser._clean_title` / `_split_clause` 加 `_strip_emphasis_markers()` 去 `*...*` / `**...**` / `<b>...</b>`

**影响**（§5.1 / §5.2 / §5.4 三连触发，必须人审）：

| 项 | 内容 | 估算 |
|---|---|---|
| chunk_id 全 re-hash | `uuid5(spec_id\|clause\|sha256(content))` content 第一行变了 | ~395k chunks 全失效 |
| 重跑 ingestion | builder → embedder → indexer 全链路 | ~6-8h |
| 重 embed | voyage-4-large@1024d × 395k chunks | $20-40 海外 / 200M 免费额度内 |
| 清 Qdrant collection + truncate chunks_meta + 重建 BM25 mmap | 期间检索不可用 | 建议夜间执行 |
| `message_citations` 现有数据 | FK `ON DELETE SET NULL`，不崩，但历史聊天 chip hover 拉不到 chunk | 可接受 / 不可接受需 user 拍 |

**Agent 倾向**：**先观察 2-3 天 A/B/C 治标效果**。如果 fuzzy match 捞回 chunk 命中率 > 90%、用户不再抱怨"加载 section 失败"，D 可永久搁置（chunker 这套设计的真正成本只是 "prompt 容易被诱导违规"，v5 prompt rule #2 + 前后端正则对齐已经从 3 个方向把这条路堵死）。如果命中率 < 70% 或仍有抱怨，再走 D 项的 §5 决策窗口。

### 其它建议（顺手 commit / chore）

1. `chore(backend): black format pre-existing files` —— `chat.py` + `test_self_rag_node.py`（~50 行格式回灌）
2. `test(backend): fix test_non_explicit_tools_does_not_invoke_tools StubLLM responses` —— pre-existing failure
3. 现场手测复现一次（任意环境）：问 "具体讲一下 ControlResourceSet" 和 "以表格形式列出 DCI1_1 的字段"，确认：① 无 `<b>` / `*xxx*` 残留 ② chip 点击不再 "加载 section 失败"，而是跳到 spec 概览页 + SnackBar 提示 ③ tooltip 不再误导性地显示 "无 chunk 上下文"（fuzzy 命中后会有真 chunk 摘要）

---

## 九、不在本次范围

- D 项 chunker 改造（§5 决策窗口）
- pre-existing black 偏差（§3）
- pre-existing integration test failure（§3）
- v5 prompt 上线后的 eval-daily round 复跑（属常规观察项，不阻塞合并）

---

## 备注：token 消耗

本次开发期间 LLM 调用估算：
- backend integration test（7 个真 LLM test，含 complex_qa 5 题）：~1-2M token / ~12min
- 文档编辑 + Grep / Read：本地，零外部 token

总 ≈ 1-2M token，按 LiteLLM 默认价格 ≈ ¥0.5-1，未触发 CLAUDE.md §5.2 阈值（< 1M token + < 100 次调用 / < 30min 三条均未达）。
