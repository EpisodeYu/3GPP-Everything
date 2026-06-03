# 对比测试：3GPP-Everything (A) vs 华为 Telco-RAG (B)

> 目标：在**中立题集**上,用 LLM 多指标(绝对 + 成对盲评)对比本项目与华为开源 3GPP RAG 的答题质量。
> 决策与探索记录见 git 提交历史 + 项目记忆 `project_huawei_telcorag_baseline`。

## 1. 对手是谁

**B = 华为开源 Telco-RAG**(`github.com/netop-team/Telco-RAG`,Huawei Paris Research Center;
作者 Bornea/Ayed/De Domenico/Piovesan/Maatouk;arXiv 2404.15939;MIT)。同一 `netop-team`
也发布了 **TeleQnA**——故本基线与该数据集同源。

接入面:FastAPI `POST /process_query/`,body `{query, model_name, api_key}` →
返回 `json.dumps({result, retrieval, query})`(整段答案 + 检索上下文)。connector =
`telcorag_client.py`。

## 2. 数据流

```
R18 交集 spec 原文 ──采样段落──▶ LLM 生成 Q+expected_facts(闭卷+R18核验+对称门) ──人审──▶ golden_compare.yaml(100)
        │
        ├─▶ A: 本项目 RAG(eval/runner.py, gen=mimo-v2.5-pro) ─▶ {answer, contexts, cited_specs}
        ├─▶ B: 华为 Telco-RAG(in-process, gen=gpt-4o-mini) ──▶ {answer, contexts, cited_specs}
        └─▶ C: 裸 LLM(deepseek-v4-pro, 无检索) ─────────────▶ {answer, claimed_spec}
                                                         │
        ┌────────────────────────────────────────────────┴───────────────────┐
  绝对指标(三系统各打,裁判 glm-5.1)              成对盲评(匿名甲/乙,正反两序,glm-5.1)
  fact_coverage / spec归属 / fact-in-context        A-B / A-C / B-C → WIN/TIE/LOSE
  recall(检索专项,LLM-free) / 利用率 / 拒答
        └────────────────────────────────────────────────┬───────────────────┘
                          compare_report.md(RAG vs 裸LLM 头条 + 3×3胜率矩阵) + Langfuse
```

> 详细评测层设计(3 系统 + 指标取舍 + 决策依据)见 **§8**。

## 3. 公平性约定(2026-06-02 与人确认)

| 维度 | 约定 | 理由 |
|---|---|---|
| **题源** | **100 题全中立自产**,不用 TeleQnA | TeleQnA 是 B 的北极星 benchmark,有主场分布偏向 |
| **覆盖** | 只采样 **A∩B 的 R18 交集(385 篇)** | B 离线库仅 R18;题落交集外则一方无库可检 |
| **B online 增强** | **关掉**(纯离线 RAG) | 引入非确定性 + 外网依赖,削弱可复现与"库检索质量"聚焦 |
| **生成 LLM** | 两边各用**产品默认**(A=mimo-v2.5-pro / B=gpt-4o-mini) | 比"产品 vs 产品";同-LLM 消融留作后续(见 §8) |
| **裸 LLM 基线** | C=`deepseek-v4-pro`,**无检索**(2026-06-03 加,见 §8) | 测 RAG 是否真有用,还是 LLM 预训练就会 |
| **裁判 LLM** | `glm-5.1`(与 A=mimo / B=gpt-4o-mini / C=deepseek 都不同源) | 避免同源偏袒;deepseek 现为被评系统 C,不能再当裁判 |

> 硬污染已排除:Telco-RAG 论文中 NN-router 用合成 3 万题训、超参用独立 2000 题
> optimization set 调,TeleQnA 仅作留出评测。我们换中立题集进一步消解软偏向。

## 4. R18 交集 spec 清单(采样范围)

`r18_intersection_specs.txt`(385 篇)由 `build_intersection.py` 可复现生成
(A=`INGEST_DATA_DIR/bm25/voyage/by_spec` 文件名 ∩ B=HF `netop/Embeddings3GPP-R18`
的 `Documents/*.docx`)。series 分布:

```
29:95  24:54  38:53  23:46  33:36  28:20  36:20  32:19  26:15  37:13  22:10  31:3  27:1
```

### 100 题配额(占比 × 重要度,**待 3GPP 专家微调**)

| series | 含义 | 配额 | | series | 含义 | 配额 |
|---|---|--:|---|---|---|--:|
| 23 | 系统架构(stage-2,核心) | 20 | | 32 | 计费/OAM | 4 |
| 38 | 5G NR | 16 | | 26 | 媒体/编解码 | 4 |
| 29 | stage-3 协议/API | 16 | | 37 | 多 RAT | 3 |
| 24 | NAS 协议 | 12 | | 22 | 业务需求 | 3 |
| 33 | 安全 | 10 | | 31 | USIM | 1 |
| 36 | LTE | 6 | | 28 | 管理/OAM | 5 |

> 相对纯占比:压低 29(stage-3 偏细碎)、抬高 23/38(架构+无线,问得最多、最有代表性)。
> 类别(definition/procedure/table/formula/multi_section)在生成阶段再做二次配比。

## 5. 起 Telco-RAG 本地服务

```bash
# 代码已在 /data/telco-rag(项目外,21G 卷);R18 库(3.3GB)下到 Telco-RAG_api/3GPP-Release18
cd /data/telco-rag/Telco-RAG_api
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 注:pin 旧版,独立 venv
# 离线库已由 huggingface_hub 预下载到 ./3GPP-Release18(setup.py 检测到则跳过)
.venv/bin/uvicorn api.deploy_api:app --host 0.0.0.0 --port 8000
```

connector(从本仓库)调用:

```python
import httpx
from eval.huawei_compare.telcorag_client import collect_baseline
from eval.settings import get_settings

s = get_settings()
async with httpx.AsyncClient() as c:
    answers = await collect_baseline(
        [("def-001", "What is a PDU session in 5G?")],
        client=c, base_url=s.resolved_telcorag_base_url,
        model_name=s.telcorag_model, api_key=s.openai_api_key,
    )
```

`OPENAI_API_KEY`(对外密钥,人提供)、`TELCORAG_BASE_URL`、`TELCORAG_MODEL` 走 `.env` / EvalSettings。

## 6. 运行约束(实测)

- **B 批量采集不走 HTTP 服务**:本机 harness 不允许常驻后台服务(uvicorn 被杀 exit 144)。
  改为在 telco venv 里 **in-process 调 `api.pipeline.TelcoRAG()`**(见 `/data/telco-rag/smoke_b.py`):
  ```bash
  cd /data/telco-rag/Telco-RAG_api && \
  OPENAI_BASE_URL="https://api.apiyi.com/v1" OPENAI_API_KEY="<relay key>" \
  KMP_DUPLICATE_LIB_OK=TRUE PYTHONPATH=/data/telco-rag/Telco-RAG_api \
  /data/telco-rag/.venv/bin/python /data/telco-rag/smoke_b.py "问题..."
  ```
  `telcorag_client.py`(HTTP connector)保留,仅当有人手动起 `uvicorn api.deploy_api:app` 时用。
- **A 采集**:`eval.runner.call_agent`,base=`http://<tgpp-net 容器IP>:8002`,token 见 live-eval 记忆。

## 7. 进度

- [x] 探索 + 确认对手 = Telco-RAG;R18 交集枚举(385)
- [x] connector `telcorag_client.py` + 单测(9);可复现交集脚本 `build_intersection.py` + 单测(3)
- [x] Telco-RAG 本地跑通(venv + R18 库 + 6 处 Windows 路径 patch + 关 online + 中转 LLM)
- [x] **双系统 1 题冒烟通过**(2026-06-02):A、B 均出答案 + 检索依据
- [x] B 批量采集脚本(in-process,telco venv → results.json)
- [x] **100 题中立题集生成**(2026-06-03):生成器 `gen_questions.py` + `golden_compare.yaml`(validate OK)— **待人领域终审**(见 CONTEXT.md §8)
- [x] **评测层方案定稿**(2026-06-03,见 §8)— 待人 approve 后开建
- [ ] 评测层实现:`collect_c` + `merge`(扩3路) + `pairwise_judge.py` + `compare_eval.py` + `compare_report.py` + 单测
- [ ] 全量跑 A/B/C → 对比报告 + Langfuse
- [ ] 回归(lint + 单测)

## 8. 评测层设计(定稿 2026-06-03,实现待 approve)

> 经多轮与人确认(裸 LLM 基线、裁判换型、faithfulness 取舍、闭卷/对称门)收口的最终方案。
> 实现交付物清单与构建顺序见末尾。

### 8.1 三个被评系统

| 系统 | 是什么 | 生成 LLM | 检索 |
|---|---|---|---|
| **A** | 本项目 3GPP-Everything RAG | mimo-v2.5-pro | 本项目流水线 |
| **B** | 华为开源 Telco-RAG | gpt-4o-mini | R18 离线库(关 online) |
| **C** | **裸 LLM 基线**(无 RAG) | deepseek-v4-pro | 无 |

**C 的意义(人 2026-06-03 提出)**:对照"RAG 是否真有用,还是 LLM 靠预训练就会"。
C 提示 = `你是 3GPP 标准专家,凭知识回答;正题须报出确切 TS 号;若非 3GPP 规范或不确定就明说`
(给 C 与 RAG 同等的拒答机会 + 让 spec 归属可比)。
**实测结论**(强制报 spec 的 7 题抽样):C 有大量 3GPP 知识、知名 spec(28.532/23.015/38.214)能报准,
但**冷门题报成同域兄弟篇**(24.483→24.282、33.928→33.107)、答案似是而非且无法溯源 →
RAG 价值在**长尾精确性 + 可溯源可信 + 不知道时拒答**,而非"裸 LLM 完全不会"。

### 8.2 指标(头条 + 诊断;裁判 glm-5.1)

| 指标 | 隔离/测什么 | 覆盖 | LLM 干扰 |
|---|---|---|---|
| **fact_coverage**(vs 金标准 expected_facts) | 端到端正确性 | A/B/C | judge |
| **spec 归属命中**(expected_spec ∈ 系统 spec 集) | 能否报对篇/可溯源 | A/B/C | 无(纯比对) |
| **fact-in-context recall**(facts 是否在检索 context 里) | **检索工程**(料检索到没) | A/B | **无(substring)** |
| **利用率** = fact_coverage ÷ recall | 给定检索后生成(prompt+LLM)的发挥 | A/B | 派生 |
| **negative 拒答**(16 题 3 方同口径) | 幻觉/守边界 | A/B/C | judge |
| **成对盲评**(匿名甲/乙,位置对冲,参考引导) | 综合质量;A-B/A-C/B-C 各正反两序 | A/B/C | judge |

**裁判换型**:原定 deepseek-v4-pro,但它现为系统 C → 改 `glm-5.1`(与三方都不同源)。

**faithfulness/ragas 去掉**(人 2026-06-03 质疑):它是**内部指标**(答案是否忠于"自己检索到的"
context),纠缠 生成LLM行为 + prompt约束 + 检索质量,**不测对错、纳不进 C、不能隔离 prompt**。
其有用部分(检索是否把料捞到)已被 **LLM-free 的 fact-in-context recall** 取代。

**检索 vs 生成的拆解**:
- 检索到了但答案没说 → 生成/prompt 问题
- 检索没到、答案也没说 → 检索问题
- 检索没到、答案却对 → 模型靠预训练记忆(RAG 没起作用,向 C 靠拢)

**prompt-vs-LLM 彻底拆分**(同-LLM 消融:同一中立 LLM 喂各自 context/prompt)成本大,**本期不做**,留后续。

### 8.3 头条故事
A/B(RAG) vs C(裸) 在 **fact_coverage + spec 归属** 上,**按 spec 冷门度拆**(核心 23/38 vs 长尾
28/32/26)→ 直观回答"RAG 值不值";配 fact-in-context recall(检索是否到位,零 LLM 干扰)。

### 8.4 交付物 + 构建顺序
- 文件:`golden_to_questions.py`(golden→问题jsonl) / `collect_c.py`(裸 deepseek) /
  `merge_results.py`+`schema.align`(扩 3 路) / `pairwise_judge.py`(位置对冲+glm-5.1) /
  `compare_eval.py`(绝对 judge 切 glm-5.1 + 检索专项 + 成对 → `scores.json`) /
  `compare_report.py`(scorecard + 3×3 胜率 + 头条 + 拆解) / 单测 / 文档。
- 顺序:① 代码+单测全绿 → ② smoke(3 题) → ③ 全量后台(A/B/C×100,B 慢 ~25min,~3-4M token 免审内) → ④ 回归。
- 成本头 = 成对盲评(3 对×100×2 序≈600 次)。压成本可选:只跑 A-B/A-C,或单序(牺牲位置去偏)。
