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
R18 交集 spec 原文 ──采样段落──▶ LLM 生成 Q+expected_facts(中立第三方法) ──人审──▶ golden_compare.yaml(100)
        │
        ├─▶ A: eval/runner.py(现成) ───────────────▶ {answer_A, citations_A}
        └─▶ B: telcorag_client → /process_query/ ──▶ {answer_B, retrieval_B}
                                                         │
                       ┌──────────────────────────────────┴───────────────┐
            绝对指标(两边各打)                       成对盲评(伏名 甲/乙, 正反两序)
   fact_coverage/faithfulness/completeness/relevance/拒答    A_WIN/TIE/B_WIN
                       └──────────────────────────────────┬───────────────┘
                                       compare_report.md + 胜率矩阵 + Langfuse
```

## 3. 公平性约定(2026-06-02 与人确认)

| 维度 | 约定 | 理由 |
|---|---|---|
| **题源** | **100 题全中立自产**,不用 TeleQnA | TeleQnA 是 B 的北极星 benchmark,有主场分布偏向 |
| **覆盖** | 只采样 **A∩B 的 R18 交集(385 篇)** | B 离线库仅 R18;题落交集外则一方无库可检 |
| **B online 增强** | **关掉**(纯离线 RAG) | 引入非确定性 + 外网依赖,削弱可复现与"库检索质量"聚焦 |
| **生成 LLM** | 两边各用**产品默认**(A=mimo-v2.5-pro / B=gpt-4o-mini) | 比"产品 vs 产品";可选再跑同-LLM 变体隔离纯检索 |
| **裁判 LLM** | `deepseek-v4-pro`(与 A/B backbone 都不同源) | 避免同源偏袒 |

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
- [ ] B 批量采集脚本(in-process,telco venv → results.json)
- [ ] 100 题中立题集生成(R18 交集采样 → LLM 生成 → 人审)— **暂缓,等人发话**
- [ ] 成对盲评 judge `pairwise_judge.py`(位置对冲)+ 绝对指标 judge 复用/扩展
- [ ] 全量跑 A、B → `compare_report.py` 对比报告 + Langfuse
- [ ] 回归(lint + 单测)
