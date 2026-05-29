# 2026-05-29 · clause 字母后缀（5.7a / B.3a）解析修复

> 触发：用户复现"什么是 DRX"问答，看到 `[6]` `[7]` 实际对应 38.321 §5.7a / §5.7b
> （MBS Broadcast / Multicast DRX）的 chip 标签只显示 "38.321"，缺 §5.7a/§5.7b；
> 单击 chip 走"未关联具体章节"分支，提示已退到 spec 概览。
>
> 锚：`CLAUDE.md` §4.2 / §8；`docs/03-development/02-ingestion-and-indexing.md §4.1`
> （Section schema）+ §7.1（已知数据债）。

---

## 一、根因（1 处正则，2 层数据）

| 层 | 现象 | 实证 |
|---|---|---|
| **解析层** | `ingestion/hf_loader/markdown_parser.py::_CLAUSE_RE` 原正则 `^([A-Z]?[\dA-Z][\d.]*)\s+(.+)$` 里的 `[\d.]*` **不收字母后缀**。`## 5.7a Discontinuous Reception for MBS Broadcast` 进 regex 后 backtrack 全部失败 → `_split_clause` 返回 `("", title_raw)` | `chunker/builder.py::_make_parent_section_id` §400 注释早就点过这个 workaround："GSMA marker 解析后部分 Annex / 字母后缀 clause（'5.15.11.5a'）会被 markdown_parser 归入 clause=''" |
| **存储层（PG / Qdrant / BM25）** | clause 为空 → `_split_section_path(clause)` 返回空 tuple → `chunks_meta.section_path = []`、Qdrant payload `section_path = []`、BM25 metadata 同步空 | 现网 PG 抽查：`SELECT ... WHERE section_path 空 AND section_title ~ '<新 regex>'` 命中 **5826 chunk × 190 spec** |
| **渲染层** | backend 检索 → Qdrant payload 给空 section_path → `parse_citations` 把空 section_path 写入 `MessageCitation` 行 → 前端 `CitationElementBuilder` 走 `byRank` 拿到空 sectionPath → `_formatLabel` 退化为只显 `spec` → `jumpToReader` 因 `cleaned.isEmpty` 走 "未关联章节" 分支 | 用户截图：chip "38.321 [6]" / "38.321 [7]"；hover 预览体能正常显示（因为预览体走 `/chunks/{chunk_id}` 取的是 chunk content，与 section_path 无关）|

## 二、本次完成（forward fix，commit `62f608b`）

### A. 正则重写

`ingestion/hf_loader/markdown_parser.py::_CLAUSE_RE`:

```python
_CLAUSE_RE = re.compile(
    r"^([A-Z](?:\.\d+[a-z]*)*|\d+[a-z]*(?:\.\d+[a-z]*)*)\s+(.+)$"
)
```

设计：

- 分两支：annex（`A` / `A.1` / `B.3a` / `A.1.2`）或编号（`5` / `5.1` / `5.7a` / `5.15.11.5a` / `5.7a.1`）
- **去掉 IGNORECASE**：annex 字母按 3GPP 约定恒大写、clause 字母后缀恒小写。
  避免 `Foreword` / `Annex A (informative)` 这类纯文本标题被误识为 clause（连带 _split_clause
  里的"至少含一个数字"安全网共同把关）。
- 含 `-` 的伪 step-list 标号（`14a-c.`）仍然不匹配，回归测试覆盖。

### B. 回归测试（7 个新 case，`tests/unit/test_markdown_parser.py::TestClauseLetterSuffix`）

- `test_letter_suffix_at_tail`：`5.7a Discontinuous Reception for MBS Broadcast` → `clause="5.7a"`
- `test_letter_suffix_in_middle`：`5.15.11.5a Sub-clause` → `clause="5.15.11.5a"`
- `test_letter_suffix_followed_by_number`：`5.7a.1 First sub of 5.7a` → `clause="5.7a.1"`
- `test_annex_with_letter_suffix`：`B.3a Annex subsection with letter` → `clause="B.3a"`
- `test_plain_clauses_still_parse`：`5` / `5.1` / `5.1.2` / `A.1.2` 不退化
- `test_pure_text_heading_not_matched`：`Foreword` / `Annex A (informative)` 仍 `clause=""`
- `test_step_list_text_not_matched_as_clause`：`14a-c. If the AMF...` 不被误识为 clause

全过：30/30 `test_markdown_parser.py`；306/306 ingestion unit；lint 过。

### C. 文档同步

- `docs/03-development/02-ingestion-and-indexing.md §4.1` Section schema 里 `clause` 字段说明
  扩到 "支持普通编号 / 字母后缀 / 附录"，给出具体例子
- 同文档新增 §7.1「已知数据债」表格，列出本次的数据债与清理计划

## 三、未完成：现网数据债（5826 chunk × 190 spec）

**本 commit 只动 ingestion 解析层；现网 PG / Qdrant / BM25 里既有的 5826 chunk
的 `section_path` 仍为 `[]`，因为它们是用旧 regex 入库的。**

按受影响 chunk 数排序的前 10 spec（基于现网 PG 实测）：

| spec_id | 受影响 chunk 数 | 典型场景 |
|---|---:|---|
| 36.523-1 | 1693 | LTE testing |
| 23.237 | 301 | SR-VCC |
| 38.331 | 301 | NR RRC（含 IE 章节）|
| 36.331 | 271 | LTE RRC |
| 29.212 | 259 | Gx interface |
| 38.321 | 233 | **NR MAC（含 §5.7a/5.7b MBS DRX，本次用户复现的）** |
| 23.002 | 216 | network arch |
| 24.008 | 193 | MM/CC/SM |
| 36.300 | 151 | LTE overall |
| 28.105 | 127 | management |
| ... | ... | （还有 180 个 spec，尾部小数量）|

### 决策（user 2026-05-29 4:20pm）：**不主动修，等下次全量 ingest 自然清**

用户原话："先把这些都更新到文档吧，等下次 ingest"。

不走主动清理的理由：

- 触发 `CLAUDE.md` §5.5（"改产品决策"，含改动 ≥ 2 模块 metadata 一致性）+ §5.2（重 ingest 有
  Voyage embed 费用）
- 受影响 chunk 不会"答错"，只会"chip 标签少 §xxx"——hover 预览仍能看到内容，回答正文不受影响
- 下次为了别的需求（新 spec / 新 Rel / chunker 大改）跑 ingest 时顺带就修了

### 临时影响面（在数据被清理之前）

- 字母后缀章节的 chip 标签缺 §xxx，单击退到 spec 概览页 + SnackBar 提示
- 主要影响 38.321 MBS DRX、5.1.4a 2-step RA、36.331 / 38.331 部分 IE 章节
- 不影响：（1）回答正文准确性；（2）hover 预览展示；（3）非字母后缀章节的 chip

### 未来如果要主动清理（备忘）

三个层都要同步打补丁，**不需要重 embed**（content / vector 不变，只改 metadata）：

1. PG `chunks_meta.section_path`：用新 regex 重 parse 现存空 section_path 的 section_title，UPDATE
2. Qdrant payload `section_path`：`PUT /collections/{c}/points/payload`，同一组 point_id 批量改
3. BM25 metadata：如果索引器序列化的就是 section_path，需要 rebuild BM25 索引（不重 embed）

或者最省事：跑一次受影响的 190 个 spec 的 reindex，所有层一并刷新（但会触发 §5.2 真 Voyage 调用）。

---

## 四、自跑验证步骤（事后复现用）

```bash
# 1. 解析层正则
cd ingestion && uv run pytest tests/unit/test_markdown_parser.py::TestClauseLetterSuffix -v

# 2. 全量 ingestion unit
cd ingestion && uv run pytest tests/unit/ -q   # 306 passed

# 3. 现网数据债扫描
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec -T postgres \
  psql -U tgpp_app -d tgpp_everything -c "
    SELECT spec_id, COUNT(*) FROM chunks_meta
    WHERE (section_path IS NULL OR json_array_length(section_path) = 0)
      AND section_title ~ '^([A-Z](\.[0-9]+[a-z]*)*|[0-9]+[a-z]*(\.[0-9]+[a-z]*)*)[[:space:]]+.+'
    GROUP BY spec_id ORDER BY COUNT(*) DESC LIMIT 30;"
```

## 五、关联 commit / PR

- `62f608b` fix(ingestion): clause 正则支持字母后缀（5.7a / 5.15.11.5a / B.3a）
- 关联前序：`eb23074` feat(frontend): chip 标签加 [N] 后缀区分同 section 不同 chunk
  （即用户当下能看到 chip 标签的前提；那个 commit 把 chip label 从 `spec §section` 改成
  `spec §section [N]`，今天暴露字母后缀 section 缺失正好因为 chip 标签变明显了）
