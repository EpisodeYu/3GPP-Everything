# 华为对比测试 — Agent 快速上手（START HERE）

> 给后续 agent 的**操作交接**文档。设计与公平性**论证**在 [`README.md`](./README.md);
> 跨会话要点在项目记忆 `project_huawei_telcorag_baseline` / `project_live_eval_deploy_ops`。
> 本文档只讲"现在是什么状态、怎么接着跑"。最后更新：2026-06-02。

## 0. 一句话

对比 **A=3GPP-Everything(本项目)** vs **B=华为开源 Telco-RAG**,在**中立题集**上用 LLM
多指标(成对盲评 + 绝对指标)评分。题源不用 TeleQnA(B 主场偏向),改从 **A∩B 的 R18 交集
spec** 自产。

## 1. 当前进度

| 状态 | 项 |
|---|---|
| ✅ | 确认 B = `github.com/netop-team/Telco-RAG`(华为);R18 交集枚举 **385 篇** |
| ✅ | Telco-RAG 本地跑通(见 §4 的坑);中转 OpenAI key 接好 |
| ✅ | **采集层**:`collect_a.py`(A) + `collect_b.py`(B,in-process) + `merge_results.py` → 统一 `results.json` |
| ✅ | 单测 22 条全绿(`eval/tests/unit/test_{telcorag_client,build_intersection,huawei_compare_schema}.py`) |
| ✅ | **3 题端到端冒烟通过**:A、B 都出答案+检索,merge 出统一 results.json |
| ✅ | **生成器** `gen_questions.py` + `gen_prompts.py` + 单测 22 条 —— A chunk 采样 + B R18 全文核验公平 + 排测试/RF spec |
| 🟡 | **正式 100 题已生成**(2026-06-03)→ `golden_compare.yaml`(100,validate OK 0 warn);**待人做领域终审**(见 §8) |
| ⏸️ | **评测层**(成对盲评 judge + 绝对指标 judge + 报告)— 后面单独做 |

## 2. 文件地图

```
eval/huawei_compare/
├── CONTEXT.md            ← 你在读(操作交接)
├── README.md             ← 设计 + 公平性论证 + 100题配额草案
├── build_intersection.py ← 枚举 A∩B R18 交集(可复现)
├── r18_intersection_specs.txt ← 385 篇交集(100题采样范围)
├── telcorag_client.py    ← B 的 HTTP connector(仅当有人手动起 uvicorn 时用)
├── schema.py             ← 统一 record(SystemAnswer)+ B 引用解析 + align
├── collect_a.py          ← A 采集(eval venv,走 runner.call_agent)
├── collect_b.py          ← B 采集(★telco venv,in-process 调 TelcoRAG)
├── merge_results.py      ← 合并 A/B → results.json
├── gen_prompts.py        ← 出题 prompt 三件套(positive / false_premise / out_of_lib)
├── gen_questions.py      ← ★100 题生成器(采样→生成→R18核验→选100);见 §8
├── golden_compare.yaml   ← ★正式 100 题(golden schema,id 前缀 hc-);待人终审
└── smoke_questions.jsonl ← 3 题冒烟集(占位,非正式题集)
单测在 eval/tests/unit/test_{telcorag_client,build_intersection,huawei_compare_schema,huawei_compare_gen}.py
Telco-RAG 代码+数据在仓库外:/data/telco-rag/(venv /data/telco-rag/.venv,python3.11)
运行产物写 eval-results/(gitignore,不入库)
```

## 3. 怎么跑(三步)

```bash
# 前置:OpenAI 中转 key(对外密钥,问人;当前在 /data/telco-rag/.../.secrets.toml + 顶层 .env)
KEY=<RELAY_KEY>; BASE=https://api.apiyi.com/v1
OUT=/data/3GPP-Everything/eval-results/huawei-compare-smoke

# ① B 采集(★telco venv,in-process,无需起服务)
cd /data/telco-rag/Telco-RAG_api && \
OPENAI_BASE_URL="$BASE" OPENAI_API_KEY="$KEY" \
/data/telco-rag/.venv/bin/python /data/3GPP-Everything/eval/huawei_compare/collect_b.py \
  --in /data/3GPP-Everything/eval/huawei_compare/smoke_questions.jsonl --out $OUT/b_raw.jsonl

# ② A 采集(eval venv;A 后端容器 IP + token 见 §5)
cd /data/3GPP-Everything && \
EVAL_BACKEND_BASE_URL="http://<tgpp-net-ip>:8002" EVAL_BACKEND_TOKEN="$(cat /tmp/tgpp-eval-token.txt)" \
PYTHONPATH=/data/3GPP-Everything eval/.venv/bin/python -m eval.huawei_compare.collect_a \
  --in eval/huawei_compare/smoke_questions.jsonl --out $OUT/a_answers.jsonl

# ③ 合并 → 统一 results.json
cd /data/3GPP-Everything && eval/.venv/bin/python -m eval.huawei_compare.merge_results \
  --a $OUT/a_answers.jsonl --b $OUT/b_raw.jsonl --out $OUT/results.json
```

统一 `results.json` 结构:`{generated_at, n_items, items:[{item_id, question, A:SystemAnswer|null, B:SystemAnswer|null}]}`;
`SystemAnswer = {item_id, question, system, answer, contexts[], cited_specs[], elapsed_ms, error, meta}`。
正式跑时把 `--in` 换成 100 题题集即可,采集/合并不变。

## 4. Telco-RAG 集成的坑(已解决,改动都在 /data/telco-rag,不入我们 git)

- **venv**:python3.11,`requirements.txt` 是 UTF-16 → 转 `requirements.linux.txt`,**剔除** `docx==0.2.4`+`doc2docx`(冲突/难装),`input.py` 改 doc2docx 惰性 import。
- **Windows 路径**:6 处反斜杠字面量已 patch 成正斜杠(embeddings.py / query.py×2 / input.py×2 / get_definitions.py×2)。
- **online 增强**:`pipeline.py` 已删除 Google online 增强,只留离线 3GPP 检索(公平性 + 不需 google key + 可复现)。
- **中转 LLM**:`api/settings/.secrets.toml` 播种 relay key;起进程时 `OPENAI_BASE_URL` 重定向(openai v2 客户端无硬编码 base);embedding=`text-embedding-3-large` **dimensions=1024**(匹配预存 .npy);chat=gpt-4o-mini。
- ⚠️ **harness 不许常驻后台服务**(uvicorn 每次 exit 144 被杀)→ B 批量**不走 HTTP**,在 telco venv in-process 调 `TelcoRAG()`(`collect_b.py` 已这么做)。

## 5. A 后端(3GPP-Everything)接入

- 容器 `tgpp-api` 在 docker 网络内,8002 不 publish 到 host。取 tgpp-net IP:
  `docker inspect -f '{{index .NetworkSettings.Networks "tgpp_tgpp-net" "IPAddress"}}' tgpp-api`(**重建后会变**)。
- token:bootstrap-admin 已 409(有用户),用 `docker exec tgpp-api python -c "...签 24h JWT..."`
  (完整脚本在记忆 `project_live_eval_deploy_ops`),写 `/tmp/tgpp-eval-token.txt`,`GET /api/v1/auth/me` 验 200。

## 6. 冒烟结果(3 题,2026-06-02)

3 题(23.501 PDU Session / 38.331 RRCReconfiguration / 24.501 Registration)A、B 均成功出答案+检索。
早期质量信号:核心架构题上 B 的 NN-router 路由偏题(召回 RF/PHY 段)、答案偏泛;A 正确引 23.501。
**仅 3 题,非结论**,正式评测见 §1 待办。

## 7. 下一步

1. **人对 `golden_compare.yaml` 做领域终审**(见 §8 复跑/调参);确认后冻结为正式题集。
2. **评测层**:成对盲评 judge(位置对冲)+ 绝对指标 judge(复用 `fact_coverage_judge`/`negative_judge`/Ragas)+ `compare_report.py`。
3. 采集只读 `{item_id, question}`,把 100 题转成 collect 用的 jsonl(或让 collect 直接读 golden yaml)即可全量跑 A/B。

## 8. 100 题生成器(`gen_questions.py`)

**怎么复跑**(eval venv,seed 固定 → 可复现;~40 万 token,<5M 免审):
```bash
cd /data/3GPP-Everything && PYTHONPATH=/data/3GPP-Everything eval/.venv/bin/python \
  -m eval.huawei_compare.gen_questions \
  --out eval-results/huawei-compare-gen/golden_compare.yaml --oversample 1.4 --seed 42
# 产物(gitignore):golden_compare.yaml(选100) + .candidates.yaml(过采样132) + gen_stats.json + gen_{skipped,failed}.jsonl
# 终稿放包内:cp eval-results/huawei-compare-gen/golden_compare.yaml eval/huawei_compare/golden_compare.yaml
```

**设计**(跨会话要点在记忆 `project_huawei_100q_generation`):
- 采样脚手架 = A 的 `by_spec/*.jsonl`(R19,带 `chunk_type`);table_lookup←table / formula←formula / 其余←text。
- **闭卷门**(`question_is_closed_book`):positive 题面**禁 spec 号 + 裸 clause/table/section 号**——否则等于开卷(把答案位置喂给检索器),expected_specs 也失意义。题按概念/IE/消息/表主题问,spec+sections 只进 expected_specs 当**隐藏 ground truth**(检索器须自己找对 spec)。违例即剔。
- **R18 公平门**:每 positive 的 `expected_facts` 去 B 的 R18 全文(`Documents.db`)核验覆盖率,<0.5 判 R19-only → skip;主动拦掉 R19-only 内容。
- 排测试/RF/study spec(`is_excluded_spec`:多部件 -N + EMC/一致性单部件);配额 def22/proc20/table16/formula12/multi14 + neg16。
- **negative 对称门**(`A_Corpus` grep A 全库):negative 要公平须"两库皆无"。8 不存在概念(false_premise) + 8 **域外真实内容**(out_of_scope,非 3GPP:BGP/OSPF/IS-IS/MPLS-TP/Wi-Fi/PON/SRv6)。⚠️ **不能用 Rel-19 特性当库外**——A 是 R19 库会答对,不对称。每道探针词去 A 库核验,有实质命中(>=3 篇)即剔(本次剔 4 道,如 802.1Q/DOCSIS/SyncE 这些 3GPP 引用的外部标准)。
- 调参:`POSITIVE_{SERIES_QUOTA,CATEGORY_TARGETS}` / `R18_COVERAGE_MIN` / `NEG_*_DOMAINS|AREAS` / `EXCLUDE_SAMPLING_SPECS`。

**校验注意**:`golden validate` OK(0 warn);`golden stats` 显示 FAIL 是因它拿主集 v1.yaml 的 target 比,本集命中自定义配额,**忽略**。
