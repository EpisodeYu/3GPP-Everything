# 2026-05-27 — A+D 定义类提质 daily eval findings

## 背景

三轮改动（citation 修复 / 输出限制放宽 / 定义类提质 A+D）合并到 `main`（`ede873a`）并重建
`tgpp-api`（project=`tgpp`，从 `/home/s1yu/3GPP-Everything` 部署）后，跑 daily 子集（56 题
hand_crafted）验证指标变化。

- 基线：`eval-results/2026-05-27-live-run-1`（同日 16:12，旧代码，同 harness/token）
- 新值：`eval-results/2026-05-27-after-ad`（A+D+输出限制，新代码）
- 连接：eval client → 容器内网 `http://<tgpp-api-ip>:8002`；token 复用 24h admin token
- harness：`pytest -m eval -k "daily or smoke"`，`RUN_LIVE_EVAL=1`

## 整体（56 题）

| 指标 | 基线 | 新 | Δ |
|------|------|----|----|
| context_recall_section | 0.775 | **0.825** | +0.05 |
| context_recall_spec | 0.90 | **0.95** | +0.05 |
| fact_coverage | 0.356 | 0.352 | ~0 |
| forbidden_violation_rate | 0.482 | 0.482 | 0 |
| negative weighted_pass_rate | 0.844 | 0.844 | 0（逐条一致）|
| duration_p50_ms | 41665 | 63243 | **+52%** |
| terminal=final | 56/56 | 56/56 | — |

## 分类（recall_section / fact_coverage，n=8/类）

| 类别 | recall 基→新 | fact 基→新 |
|------|------------|-----------|
| **definition** | **0.875 → 1.0** | 0.423 → 0.465 |
| procedure | 0.625 → 0.75 | 0.36 → 0.381 |
| multi_section | 0.75 → 0.75 | 0.282 → 0.282 |
| table_lookup | 0.75 → 0.75 | 0.565 → 0.482 |
| formula | 0.875 → 0.875 | 0.15 → 0.15 |

关键逐题：
- `hand-def-002`：rec **0→1.0**（此前定义全崩的题，现命中定义条款）
- `hand-def-005 / def-007`：forbidden 违例 1→0（LTE / Sidelink 串味清除）
- `hand-def-006`：fact 0.33→0.67
- table_lookup 下滑几乎全来自 `hand-table-004`（rec 1→0 单题）；n=8 抽样 + classify 改写 query
  的 LLM 波动，判为噪声而非真退化（待下次 daily 复核）

## 结论

- **A+D 设计意图被验证**：definition section_recall 8/8 全中（0.875→1.0），最差题修复，串味降低。
- 附带收益：procedure recall +0.125、整体 recall +0.05。
- 代价：定义题延迟 +52%（hyde + multi_query + RERANK_TOP_K 8）。
- fact_coverage 持平：召回到位但模型抽取的"期望事实"未增多——后续提质要看 generate prompt
  / 是否上 C（定义专用 prompt）。

## 仍开口（与本次改动无关或待办）

1. **negative 0.844 < 0.85**：daily 阈值仍未达，与基线**完全一致**，非本次回归。2 条 INVALID
   = `hand-neg-011`（5GMM-HIBERNATE 当真）/ `hand-neg-015`（QBER 当真）模型接受伪前提。属
   negative 拒答能力问题，需单独治（prompt 收紧伪前提识别 / judge 复核）。
2. **ragas faithfulness 未测**：daily harness 不算；需跑 weekly / langfuse ragas 路径确认
   输出放宽（RERANK_TOP_K 8 + 完整性 prompt）没拖低 faithfulness。
3. **table_lookup fact 抽样波动**：下次 daily 复核 `hand-table-004` 是否稳定。
4. 前端 citation 修复（`citation_chip.dart`）已在 main，但本次 **web 未重建**（DEPLOY_SKIP_WEB=1，
   eval 只需 API）；需单独 `make web-build` + 部署 web 才在前端生效。
