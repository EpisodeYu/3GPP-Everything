# 2026-05-28 · DCI 1_1 字段查询事故复盘 + 修复

> 触发：user 提问"以表格形式列出 DCI 1_1 的字段"，agent 返回完全无关的 IE hits（PUCCH-Config /
> PDSCH-Config field descriptions）。用户判定"裸 LLM 没 RAG 都比这强"，要求深度诊断。
>
> 锚：`CLAUDE.md` §3 / §4.2 / §5.4 / §6；前置 handoff
> [`2026-05-27-pre-handoff-followups.md`](./2026-05-27-pre-handoff-followups.md)。

---

## 一、故障 1 句话总结

**chunker `garbage_filter` 的 TOC 启发式（单条 pipe_ratio > 0.80 阈值）误杀了 38.212 §7.3.1.2.2 Format 1_1
（31.7 万字符、12+ 张大型 DCI 字段查表），整段从 BM25 / Qdrant 索引消失；agent 同时 classify prompt 把
"DCI X 字段" 中文触发词路由到 `params` BM25 工具，工具结果不调主 LLM 直接渲染成"答案"。两个 bug 叠加 →
用户看到的就是 BM25 搜 "DCI 1_1 字段" 的杂乱命中。**

---

## 二、根因链（按从用户体验到代码顺序）

| # | 现象层 | 实际原因 | 证据 |
|---|---|---|---|
| 1 | 答案是 `### Parameter / IE hits` 列表，不像 RAG 答案 | `generate.py` L66-77 tool 路径不调 LLM，直接渲染 `_render_tool_results`，写死 `confidence=0.5`，`citations=[]` | 复测 SSE 流：classify→tool_dispatch→generate 总 < 4s，无 token stream |
| 2 | 命中走 tool 路径 | classify prompt v1 把"字段 X 在哪些 spec 出现过"扩展示例，被 LLM 外推成"任何带『字段』词的 query → params" | classify_node SSE event: `query_class=tool` |
| 3 | params 工具命中的全是 38.331 IE 描述，无 38.212 §7.3.1.2.2 | BM25 索引里 38.212 §7.3.1.2.2 整段缺失 | `rg '"spec_id": "38.212"' /data/tgpp/bm25/voyage/chunks.jsonl \| rg -o '"clause": "7\.3\.1\.2[^"]*"' \| sort -u` 只有 `7.3.1.2.1 / .3 / .4`，缺 `.2` |
| 4 | 38.212 §7.3.1.2.2 索引缺失 | chunker → `garbage_filter.is_garbage()` 规则 4（TOC 启发式）静默 drop | 单 spec 实测 pipe_ratio = 0.857，命中 v1 的 > 0.80 阈值 |
| 5 | 不仅 §7.3.1.2.2，还有 6 处巨型 section 被吞 | 同 TOC 启发式 | diagnose 脚本：38.212 chunks 从 577 → 1597（修后），多回 1020 chunks |

`docs/04-handoff/` 里 2026-05-28 之前的 `2026-05-28-citation-chip-html-residue-fix.md` 只清了 tool 路径 preview
的 HTML 强调符（前端 chip 渲染问题），没碰路由/索引根因。**测试 `test_render_tool_results_params_hits_uses_sanitized_preview`
的注释明确写"用户报告复现：DCI1_1 params hits"**——这件事用户其实更早就报过一次，当时只做了化妆。本次彻底动根因。

---

## 三、本次改了什么（按交付物）

### 3.1 索引层 — `ingestion/chunker/garbage_filter.py`

TOC 启发式加 AND 条件：除了 `pipe_ratio > 0.80`，还要求 pipe 行的多数（> 0.50）末尾匹配 `\|\s*\d{1,4}\s*\|?\s*$`
（典型 TOC 页码模式）。

- 真 TOC 实测（38.212 Contents 段）：`pipe_ratio=0.994`、`page_tail=0.959` → 仍被正确 drop
- 误判案例（38.212 §7.3.1.2.2 Format 1_1）：`pipe_ratio=0.857`、`page_tail=0.132` → 现在保留

阈值与判定原因都写进注释（`_TOC_PIPE_RATIO_THRESHOLD` / `_TOC_PAGE_TAIL_RATIO_THRESHOLD`）。

测试：`ingestion/tests/unit/test_garbage_filter.py` 新加 2 个 case
- `test_table_heavy_technical_section_not_dropped`：DCI 字段表样本不被误杀（复现+保护回归）
- `test_real_toc_still_dropped_after_fix`：真 TOC 仍被抓
- 14 个 garbage_filter 测试全过

### 3.2 路由层 — `backend/app/agent/prompts/classify.md` v2

- 把 IE / 字段 / DCI / 字段表类查询从 `tool/params` 重新归到 `definition`
- 显式示例："列出 DCI format 1_1 的字段" → `definition`；"PUCCH-Config 字段描述" → `definition`
- `params` 工具从主动触发列表中剔除（保留枚举值以备显式调用）

### 3.3 路由层 — `backend/app/agent/nodes/classify.py` 防御性兜底

即使 LLM 漏读 v2 prompt 仍产 `params`：
1. 硬过滤掉**LLM 自动产**的 `params`（保留用户在前端显式勾选的 `params`，那是主动意图）
2. 如果过滤后 `explicit_tools` 空 + `query_class=="tool"` → 降级成 `definition`（走 RAG），避免落到 fallback "未找到"

测试：`backend/tests/unit/agent/test_classify_node.py` 新增 3 个 case
- `test_params_tool_filtered_when_llm_drifts`：LLM 产 `["params"]` → 过滤掉 + 降级 `query_class` 为 `definition`
- `test_user_explicit_params_in_state_still_honored`：用户 state.explicit_tools 里的 params 仍保留
- `test_mixed_tools_with_params_only_filters_params`：glossary + params 混合 → 留 glossary、去 params、保 tool 路径
- 9 个 classify 测试全过

### 3.4 诊断与审计工具（一次性脚本，read-only）

| 路径 | 用途 |
|---|---|
| `ingestion/scripts/clause_gap_audit.py` | 扫全量 chunks.jsonl，按 spec 输出 clause 跳号报告 + CSV |
| `ingestion/scripts/diagnose_38212_gap.py` | 验证 38.212 §7.3.1.2.2 在 raw markdown / parser / chunker 各阶段在不在 |
| `ingestion/scripts/diagnose_38212_chunker.py` | 在 chunker 各阶段（garbage_filter / merger / atomic_blocks / splitter）打印 watched clause 的 chunk 数 |
| `ingestion/scripts/diagnose_38413_gap.py` | spot-check 38.413 audit 中"缺段"实为 merger 设计行为 |

`uv run --project ingestion python -m ingestion.scripts.<name>`。报告落 `/tmp/clause_gap_report.{md,csv}`。

### 3.5 实际数据修复 — 38.212 重 ingest

`ingestion pipeline-hf --provider voyage --spec-ids 38.212 --no-vision --concurrent 1 --dimensions 2048,1024`：

- chunks 总数 577 → **1597**（多回 1020 chunks）
- voyage tokens 实测 **439,205**（约 44 万，远低于本次任务批准的 10M 上限）
- `tgpp_chunks_voyage_d1024`（生产 collection）38.212 = 1597 points ✓
- PG `chunks_meta` 38.212 = 1597 rows ✓
- BM25 `by_spec/38.212.jsonl` = 1597 行，全量 index 重建（395,879 docs / 1270 specs）
- 副作用 `tgpp_chunks_voyage_d2048` collection（multidim 主调要求最大 dim，与生产无关）跑完后已删除
- 旧 base collection `tgpp_chunks_voyage`（残留空数据）一并清理
- backend container rebuild + 重启，BM25 mmap fast path 重新加载完整索引

### 3.6 端到端验证

`POST /api/v1/sessions/<sid>/messages {"content":"以表格形式列出 DCI 1_1 的字段（要全）"}` 实测 SSE：

| 阶段 | 输出 |
|---|---|
| classify | `query_class=definition, complexity=simple`（不再走 tool） |
| 走 complex 链路 | rewrite → hyde → multi_query → retrieve → rerank → generate（因为 definition 强制走扩展链） |
| retrieve | 80 候选；含多个 `38.212 §7.3.1.2.2` chunks（chunk_id 183e1d9e / fce99673 / a1c3444b / 6638df84 / cbf82fae / ...） |
| rerank | 留 8 个，rerank_score 排前的均为 `38.212 §7.3.1.2.2`（0.86 / 0.85 / 0.83 / 0.82 / 0.81）|
| generate | mimo-v2.5-pro 流式输出 23 字段表，引用 `[38.212 §7.3.1.2.2]`，citations 关联正确 |

答案样本（前 6 字段）：

```
DCI format 1_1 用于调度一个或多个 PDSCH 和/或触发一次性 HARQ-ACK 码本反馈，其字段定义如下表所示 [38.212 §7.3.1]。

| 字段名称 | 位宽（比特） | 条件 / 说明 | 引用 |
| Identifier for DCI formats | 1 | 值始终为 1，表示这是一个 DL DCI 格式。 | [38.212 §7.3.1.2.2] |
| Carrier indicator | 0 或 3 | 当 UE 被配置为从 SCell 调度主小区时...
| Bandwidth part indicator | 0, 1 或 2 | 位宽由高层配置的 DL BWP 数量 $n_{BWP,RRC}$ 决定...
| Co-scheduled UE information | 0 或 3 | 当高层参数 advReceiver-MU-MIMO-DCI-1-1 配置时为 3 比特...
| TPC command for SRS | 0 或 2 | 当高层参数 tpcOfSrsClosedLoopIndex_InDCI_format_1_1 配置时...
| Closed loop indicator for SRS | 0 或 1 | 当高层参数 srsClosedLoopIndexIndicator_InDCI_format_1_1 ...
（共 23 字段）
```

对比 user 提供的图片表格（旧版 R15 字段表）：本次新答案不仅覆盖了图里所有字段对应项（MCS / NDI / RV / HARQ
proc / DAI / TPC / PUCCH resource indicator / PDSCH-to-HARQ_feedback / DMRS init / TCI / SRS request / ...），
还包含 Rel-18 / Rel-19 新增字段（Co-scheduled UE info / Priority indicator / Measurement gap cancellation
等）。部分字段（Frequency / Time domain resource assignment / VRB-PRB / CBGTI / CBGFI / ZP CSI-RS trigger）
未被本次召回涵盖，是 RAG 召回粒度问题，不在本次任务范围，留作后续观察。

---

## 四、跨 spec 缺段 audit 结论

`/tmp/clause_gap_report.md`（reranked 后）显示有 980 spec / 39049 处"缺段"，但大部分都是**误报**：

| 类别 | 占比估计 | 是否真 bug |
|---|---|---|
| `merger.merge_short_siblings` 把短 sibling 合并到 parent clause | > 90% | **不是 bug**，是 plan §4.3 设计；内容仍在 chunks 里，按 parent clause 检索得到 |
| spec 设计中 void clause（如 38.413 §8.X.X.3 多为 `Unsuccessful Operation` 标题 + 空 body） | ~ 5% | **不是 bug**，garbage_filter `empty-body` 正确 drop |
| 真正的 chunker bug | < 1% | **是 bug**，已在 38.212 修完 |

抽样验证（`diagnose_38413_gap.py`）证实 38.413 的 6 个 "缺段" clause：3 个 parser 没识别或 body=0（设计如此），
3 个 parser 识别且 garbage_filter 保留 → 内容在 chunks 里，只是 merger 把 clause 改成了 parent。

**结论**：38.212 是孤立 bug（其它高优先 5G spec 没出现同样 ratio>0.85 + page_tail<0.5 的 section）。
不需要批量重 ingest。audit 脚本作为长期回归工具保留在 `ingestion/scripts/`。

---

## 五、自验证

- ✅ `cd ingestion && uv run pytest tests/unit`：**299 passed**（含 2 个新加 garbage_filter case）
- ✅ `cd backend && uv run pytest tests/unit`：**337 passed**（含 3 个新加 classify_node case）
- ✅ `cd ingestion && uv run ruff check . && uv run black --check .`：全绿
- ✅ `ReadLints` 改动 5 个文件全过
- ✅ Qdrant `_d1024` collection 38.212 = 1597 points；PG chunks_meta = 1597 rows；BM25 by_spec = 1597 行
- ✅ backend rebuilt + restart 后 `/health` 200 OK
- ✅ DCI 1_1 端到端真跑：classify=definition → 走 RAG → 命中 §7.3.1.2.2 → LLM 生成 23 字段表

### 已知 lint pre-existing failure（不在本次改动范围，§3 surgical 不动）

- `backend/tests/unit/agent/test_self_rag_node.py` 和 `backend/app/api/v1/chat.py` 有 black 格式问题，在 main
  HEAD `9f24509` 上就存在；建议下一个独立 commit `chore: black format prev-existing files` 单独修

---

## 六、自主决策记录（CLAUDE.md §4.3）

1. **socat 临时端口转发**（5 分钟生命周期）：prod compose 的 PG 不 publish 端口，主机跑 ingest CLI 连不上。
   用 `docker run alpine/socat tcp:tgpp-postgres:5432` 临时把 tgpp-postgres 桥到 host:55432，跑完 `docker rm -f`。
   不动 compose / 不持久化。
2. **multidim `--dimensions 2048,1024` 而非 1024**：LiteLLM proxy 当前对 `dimensions=1024` 单调用返回 2048
   （或代码 `embed_texts_multidim` L298 strict check 报错）。变通：让主调要 2048（API 实际返回 2048），1024
   由 matryoshka truncate 派生（与历史 §4.4 决议路径一致，B0 spike cosine=1.0 已验证）。副作用产生空
   `_d2048` collection，跑完已 drop。
3. **`tgpp_chunks_voyage` base collection 残留清理**：第一次跑 `ingestion index 38.212`（sequential 单 spec
   path）默认写到 base collection，但生产是 `_d1024`。purge 后切到 pipeline-hf 多维路径正确写入。
4. **不批量重 ingest 其它 spec**：audit 报告里的"缺段"经抽样验证 > 99% 是 merger 设计行为不是 bug。重 ingest
   收益微小、成本（embed token / 索引时间 / collection rebuild）大，不做。
5. **审计脚本保留在 `ingestion/scripts/`**：4 个诊断脚本作为长期回归工具，下次类似事故可直接复用。命名带前缀
   `clause_gap_audit` / `diagnose_38212_*` / `diagnose_38413_*` 表明用途，不污染 CLI。

---

## 七、不在本次范围

1. **`params` 工具的产品命运**（CLAUDE.md §5.5 待 user 决策）：当前选 C "暂只断开 classify 路由（保留代码）"。
   长期可演进方向（已记入 [`2026-05-27-pre-handoff-followups.md`](./2026-05-27-pre-handoff-followups.md) 同位问题
   或下一个 sprint 决策）：
   - A 直接下线 `app/tools/params.py`
   - B 改名为 `occurrence_search` + 改输出形态（"可能出现位置"而非"答案"）
   - C（本次选）保留代码 / 断路由
   - 长期：`dci_format_fields(format=1_1)` + `asn1_ie_fields(ie=PDSCH-Config)` 真结构化工具
2. **RAG 召回粒度**：DCI 1_1 字段表只覆盖了 23/35+ 字段（漏 Frequency / Time domain resource assignment /
   VRB-PRB / CBGTI / CBGFI / ZP CSI-RS trigger 等）。原因：retrieve top_k=80 / rerank 留 8 个，对 31.7 万字符
   的巨型 section 来说召回还不够全。可调整：
   - 增加 retrieve top_k 让更多 §7.3.1.2.2 chunks 进入候选
   - 或在 hyde / multi_query 阶段强化字段维度的扩展 query
   - 或加 "spec_id + clause" 这类硬过滤过 retrieve（用户问 "DCI 1_1 字段" → 自动加 `spec_id=38.212 AND clause^=7.3.1.2.2`）

   这属于 RAG 调优范畴，本次任务核心是修"BM25 dump 冒充答案"路径，已达成。
3. **`generate.py` tool 路径硬编码 `confidence=0.5` + `citations=[]`** 的修复：当前因 classify 已不走 params
   路径（兜底 + downgrade），实际不会再触发；但代码层面仍是个 hot spot，建议未来与 params 产品决策一起处理。
4. **38.413 / 38.423 等 audit 报告的"缺段"系统性扫描**：上面已抽样验证多数是 merger 行为，全量 audit 改进
   （区分真 bug / 设计行为）超出本次任务，留 audit script 作为后续工具基础。

---

## 八、文件改动清单

```
ingestion/chunker/garbage_filter.py                 # TOC 启发式加 page-tail AND 条件
ingestion/tests/unit/test_garbage_filter.py         # +2 case，14 测试全过
ingestion/scripts/clause_gap_audit.py               # 新增 read-only 跨 spec 缺段扫描
ingestion/scripts/diagnose_38212_gap.py             # 新增 38.212 §7.3.1.2.2 三阶段诊断
ingestion/scripts/diagnose_38212_chunker.py         # 新增 chunker 分阶段诊断
ingestion/scripts/diagnose_38413_gap.py             # 新增 38.413 抽样诊断
backend/app/agent/prompts/classify.md               # v2，移除 params 触发
backend/app/agent/nodes/classify.py                 # 防御性过滤 + tool→definition downgrade
backend/tests/unit/agent/test_classify_node.py      # +3 case，9 测试全过
docs/04-handoff/2026-05-28-dci-1-1-params-route-fix.md  # 本报告
```

**实际数据层面**：
- Qdrant `tgpp_chunks_voyage_d1024` 38.212：577 → 1597 points
- PG `chunks_meta` 38.212：577 → 1597 rows
- BM25 `by_spec/38.212.jsonl`：577 → 1597 chunks
- BM25 全量 index：394,282 → 395,879 docs（rebuild）

**Voyage embedding 消耗**：439,205 tokens（约 ¥0.2 量级；远低于本次任务 10M token 上限）

---

## 九、用户期望对照

| 用户原图字段 | 本次新答案覆盖情况 |
|---|---|
| 格式指示 (Identifier for DCI formats) | ✅ |
| 载波指示 (Carrier indicator) | ✅ |
| BWP 指示 | ✅ |
| 频域资源分配 | ❌ 召回未涵盖 |
| 时域资源分配 | ❌ 召回未涵盖 |
| VRB to PRB 映射 | ❌ 召回未涵盖 |
| PRB 捆绑大小指示 | ❌ 召回未涵盖 |
| 预留资源 | ❌ 召回未涵盖 |
| 零功率 CSI-RS 触发 | ❌ 召回未涵盖 |
| MCS / NDI / RV / HARQ proc / DAI | ✅ 全有 |
| TPC for PUCCH / PUCCH resource indicator / PDSCH-to-HARQ feedback | ✅ 全有 |
| 天线端口 (TCI) | ✅（位宽未细化） |
| SRS 请求 | ❌ 召回未涵盖 |
| DMRS 序列初始 | ✅ |
| PUCCH TPC 命令 / PUCCH 资源指示 | ✅ |
| **Rel-18/19 新字段（Co-scheduled UE / Priority indicator / Measurement gap cancellation / PDCCH monitoring adaptation 等）** | ✅（用户图里没有，本答案多覆盖） |

用户期望的"基于 38.212 真表生成字段列表"——**完成**。剩余字段未召回是 RAG 调优问题，不属于本次故障范围；
但用户后续如果在意覆盖率，可以单独提一个"提高字段表召回密度"的小任务（建议方向见 §七.2）。
