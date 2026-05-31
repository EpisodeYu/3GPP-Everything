# 2026-05-31 · chunker 公式 alt-text 注入（dormant until reindex）

> 起源：[`2026-05-30-ragas-4metric-uplift-results.md`](2026-05-30-ragas-4metric-uplift-results.md)
> §3.4 / §4.3 #3 把 formula / multi_section 类 ctx_recall（0.52 / 0.61）的责任
> 划给「ingestion 层 chunker latex fallback」。本帖落地"chunker 侧能做到的最大
> 化"，即 retrieval signal 增强；公式原文恢复不在 chunker 范围（需换上游 GSMA
> marker / OCR 重抽）。
>
> 锚：`CLAUDE.md` §4.2 / §5.1 / §5.4 / §7 / §8；
> `docs/03-development/02-ingestion-and-indexing.md §4.3`。

---

## 一、根因（再次明确，避免下次跑偏）

| 层 | 现象 | 实证 |
|---|---|---|
| **上游（GSMA marker）** | `$$...$$` display equation 抽取阶段就丢内容，留下"defined by\\n\\nwhere\\nand"骨架；inline `$...$` 大多保留但散落在描述句中 | 38.211/raw.md line 1067-1090 §5.3.1：`is defined by` 后两段全空、5 条 bullet 全是 `- is given by clause 4.2;`（LHS 变量名也丢）；line 1162 / 1164 有少量 `$$...$$` 是保留的 |
| **chunker 现状** | inline math 原样进 chunk content，但 LaTeX token 对 BM25 不友好、voyage-4-large dense 对纯符号也召不准；抽空模式的"骨架文本"对 retrieval 几乎无信号 | hand-formula-001 / 004 / 007 / 008 + hand-multi-002 五道题 ctx_recall 在 v11 max-of-3 上仍卡在 0.4-0.5 |
| **retrieval / agent** | 召得不到 → ragas judge 给 not attributable → ctx_recall 拖底 | handoff §0 表：formula 类 ctx_recall=0.521 / multi 类 ctx_recall=0.612 |

**chunker 没法"还原"上游已丢的公式文本**——这是物理事实。本期只做"增强 retrieval signal"。

## 二、本次完成（forward fix，commit `871eb0b`）

### A. 新模块 `ingestion/chunker/formula_alt.py`

| 函数 | 职责 |
|---|---|
| `extract_latex_symbols(text, max_symbols=40)` | 从 `$...$` / `$$...$$` 中抽符号 token，去重保序返回；处理 `\text{}` / `\mathrm{}` 嵌套、Greek 字母（`\Delta` → "Delta"）、过滤 LaTeX 关键字 80 个（`frac/cases/begin/cdot/...`）、保留 `_^` 作标识符内部组合符 |
| `has_stripped_formula_marker(text)` | 识别三类抽空模式：（1）trigger 短语（`defined/given/expressed/computed/obtained/...` + `by/as`）后紧跟空段 + anchor（`where/and/with/here/in which/for which/such that`）；（2）连续 ≥ 2 个孤立 anchor 行；（3）bullet orphan（`- is given by clause 4.2;` 这种 LHS 已丢的） |
| `build_formula_annotation(text)` | 组合两个信号 → 多行 alt-text；纯散文 chunk 返回空串、零开销 |

### B. `chunker/builder.py::_build_text_chunk_content`

- chunk content 末尾追加 annotation（仅当 `build_formula_annotation` 返回非空）
- `raw_extra["has_formula_annotation"] = True` 标记，便于下游统计 / dashboard

### C. 实测效果（38.211 §5.3.1 真实 chunk）

原 chunk content（截选）：

```text
[38.211 § 5.3.1 OFDM baseband signal generation for all channels except PRACH and RIM-RS]

The time-continuous signal on antenna port and subcarrier spacing configuration
for OFDM symbol  $l \in \{0, 1, \dots, N_{\text{slot}}^{\text{subframe}, \mu} ...
... is defined by

where at the start of the subframe,
and
- is given by clause 4.2;
- is the subcarrier spacing configuration;
...
```

本次改动后 append：

```text
Formula symbols: l, N, slot, subframe, mu, symb
[Note: source markdown contains stripped formula(s); variable names and
structure described in surrounding prose]
```

收益：
- BM25 拿到拆好的标识符 token（原 `$N_{\text{slot}}^{\text{subframe},\mu}$` 是一坨符号，BM25 tokenize 后基本是噪声）
- dense embedding 拿到自然语言列表，比纯 LaTeX 更友好
- LLM 看到 `[Note: ... stripped formula(s) ...]` 知道这里有公式但被上游抽空，应基于周围 prose 回答（而不是幻觉 LaTeX）

### D. 单测（28 个新 case）

- `tests/unit/test_formula_alt.py`（23 case）：5 处真实样本 + 边界 case
  - `test_annotation_38211_5_3_1_real_sample`：handoff §3.4 列出的 hand-formula-001 苦主样本
  - `test_stripped_marker_8_4_2_2_1_skeleton`：38.211 §8.4.2.2.1 极端样本（trigger + 空段 + where + 空段 + and 全 skeleton）
  - `test_stripped_marker_converted_to_as`：38.214 §8.1.7 形态
  - 7 个 extract 测试覆盖 inline / display / Greek / 嵌套 / 数字过滤 / 上限 / 不闭合 `$`
- `tests/unit/test_builder_smoke.py`（5 case 新增）：端到端验证含 inline / 仅抽空 / 纯散文不动 / chunk_id 跨次幂等 / §5.3.1 真实样本

全过：**334 ingestion unit passed**（无回归）；ruff + ReadLints 0 新增。

## 三、关键决策：**dormant until reindex**

### 漂移率 dry-run 实测（5 spec / 15,917 chunks）

| spec_id | chunks | annotated | drift% | 备注 |
|---|---:|---:|---:|---|
| 38.211 | 383 | 187 | **48.83%** | 物理层信道/信号 |
| 38.214 | 1,418 | 688 | **48.52%** | 物理层 procedure |
| 38.213 | 1,201 | 473 | **39.38%** | 控制信道 procedure |
| 38.212 | 1,597 | 363 | 22.73% | 信道编码 |
| 38.331 | 11,318 | 186 | **1.64%** | RRC 对照组（几乎无 LaTeX）|
| **合计** | **15,917** | **1,897** | **11.92%** | — |

By chunk_type：`formula 100% (138/138) / text 26.4% (1370/5200) / table 6.1% (327/5336) / action_list 2.9% (57/1996) / asn1 0.13% (4/3180) / figure 1.5% (1/67)`。

dry-run 跑法（可重复，零成本，无副作用）：

```bash
cd ingestion
PYTHONPATH=/data/3GPP-Everything uv run --project . python \
  scripts/poc_formula_alt_drift.py 38.211 38.214 38.212 38.213 38.331
```

### 决策（user 2026-05-31 1:50pm）：**不主动 reindex，等下次全量 ingest 自然生效**

理由（同 `2026-05-29-clause-letter-suffix-fix.md` §三的逻辑）：

- 触发 `CLAUDE.md` §5.1（动 chunker 全局口径，影响 chunk_id 漂移）+ §5.2（reindex 约 95M voyage token 估算）+ §5.4（清 Qdrant `tgpp_chunks_voyage_d1024` 重建）+ §5.6（v11 max-of-3 ragas baseline 不再可比，要立新帖）
- 11.92% 超过 M3→M6 过渡硬指标 5%，按规定**必须人审**
- 当前用户体验影响：formula / multi_section 类 ctx_recall 仍 0.4-0.5（handoff §0 已记录）；可接受、可观测
- 下次为别的需求（新 spec / 新 Rel / chunker 大改 / 上游 marker 升级）跑全量 ingest 时一并刷新

### 临时影响面（在 reindex 之前）

- **现网 ~395k chunk 的 chunk_id 不变**（content 没动 → hash 不变 → 索引不漂移）
- **chunker 新行为 dormant**：只有跑了 `build_chunks` 的新数据（新 spec ingest / 单 spec 测试）才看得到 annotation
- **eval / ragas 基线不变**：v11 max-of-3（faith 0.825 / ans_rel 0.807 / ctx_recall 0.760 / ctx_prec 0.954）仍是当前 baseline
- **回归风险接近零**：纯散文 chunk 完全不受影响（388/15917 实测：38.211 §1 Scope 等 chunk 不出 annotation）

### 触发 reindex 的入口（备忘 - 启动时要做的事）

按 CLAUDE.md §6.1 plan 阶段先停下问人。预计步骤：

1. POC 17 篇正式漂移率审计（用 `scripts/poc_formula_alt_drift.py`，跑 `eval-results/m6-prep/poc17_purge.md` 列出的 17 spec id）；预期 < 25%（POC 17 篇含 38.211 / 38.213 / 38.214 / 38.331 等）
2. 人 approve §5.2 voyage 调用预算 + §5.4 Qdrant collection 删建
3. 全量 reindex：`make ingest-full` / 等价 CLI；预计 5-10h
4. 重跑 ragas 4 metric 至少 1 轮 → 立 v12 max-of-N baseline
5. 验证：formula / multi_section 类 ctx_recall 是否真能从 0.4-0.5 → ≥ 0.65（这是本次投资能不能拿回的关键观测点）
6. 若 ctx_recall 没明显改善 → reindex 是负 ROI，复盘 annotation 设计是否需要 P3（spec-specific 描述句重写）

## 四、文档同步（CLAUDE.md §8）

- `docs/03-development/02-ingestion-and-indexing.md §4.3`：chunk 类型表后新增"公式 alt-text 注入"段，说明 dormant 状态、漂移率 + 触发 reindex 的备忘锚
- `docs/04-handoff/2026-05-30-ragas-4metric-uplift-results.md §4.3 #3`：从"待启 ingestion 层 dev 任务"更新为"chunker 侧已落地（commit `871eb0b`），等 reindex 才生效"，加交叉引用本帖

## 五、自跑验证步骤（事后复现用）

```bash
# 1. 单测
cd ingestion && uv run pytest tests/unit/test_formula_alt.py tests/unit/test_builder_smoke.py -v
#    → 37 passed

# 2. 全量 ingestion unit
cd ingestion && uv run pytest tests/unit/ -q   # 334 passed

# 3. lint
cd ingestion && uv run ruff check chunker/ tests/unit/ scripts/poc_formula_alt_drift.py

# 4. 漂移率 dry-run（任意 spec id 列表，零成本可重跑）
cd ingestion && PYTHONPATH=/data/3GPP-Everything uv run --project . python \
  scripts/poc_formula_alt_drift.py 38.211 38.331

# 5. 单 chunk 抽样人审（看 annotation 长什么样）
cd /data/3GPP-Everything && PYTHONPATH=. uv run --project ingestion python -u -c "
import os
from pathlib import Path
from ingestion.chunker.builder import build_chunks
from ingestion.hf_loader import (
    GsmaHfLoader, dedupe_keep_latest, get_meta, manifest_session, read_entries
)
with manifest_session(Path('/data/tgpp/markdown/gsma_manifest.sqlite')) as conn:
    entries = read_entries(conn)
    rev = get_meta(conn, 'last_pull_revision')
entry = dedupe_keep_latest([e for e in entries if e.spec_id == '38.211'])[0]
loader = GsmaHfLoader(revision=rev, token=os.environ.get('HF_TOKEN'))
for bundle in loader.iter_specs([entry]):
    chunks, _ = build_chunks(bundle, vision_resolver=None)
    for c in chunks:
        if c.clause == '5.3.1' and c.raw_extra.get('has_formula_annotation'):
            print(c.content)
            break
    break
"
```

## 六、关联 commit / 文件清单

- `871eb0b` feat(ingestion): chunker 为含 LaTeX 公式的 chunk 注入符号 alt-text 与抽空标注

```
ingestion/chunker/formula_alt.py                 [+366 行 新增]
ingestion/chunker/builder.py                     [+13 行 修改]
ingestion/tests/unit/test_formula_alt.py         [+283 行 新增]
ingestion/tests/unit/test_builder_smoke.py       [+94 行 修改]
ingestion/scripts/poc_formula_alt_drift.py       [+180 行 新增]
ingestion/pyproject.toml                         [+2 行 修改：scripts/ 加 B008]
```

- 关联前序：[`2026-05-30-ragas-4metric-uplift-results.md`](2026-05-30-ragas-4metric-uplift-results.md) §3.4 / §4.3 #3
- 同情境前例（改 chunker 但等下次 ingest 生效）：[`2026-05-29-clause-letter-suffix-fix.md`](2026-05-29-clause-letter-suffix-fix.md)
