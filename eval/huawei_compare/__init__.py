"""对比基线采集：3GPP-Everything (A) vs 华为开源 Telco-RAG (B)。

- `telcorag_client`：起本地 Telco-RAG 服务后，POST /process_query/ 取开放式答案 + 检索上下文，
  统一成下游 judge（fact_coverage / faithfulness / pairwise）可消费的轻量结构。
- `build_intersection`：枚举 A∩B 的 R18 交集 spec 清单（100 题采样范围）。

设计与公平性约定见 `eval/huawei_compare/README.md`。
"""
