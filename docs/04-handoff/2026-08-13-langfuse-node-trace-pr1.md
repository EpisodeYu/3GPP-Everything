# Langfuse 节点级 trace（Issue #9 PR1）交付记录

> 日期：2026-08-13
> 范围：只完成 LangGraph node-level trace wiring；LiteLLM generation / token / usage / cost 属于 PR2。

## 行为契约

- 每个 assistant run 在 graph 启动前得到稳定的 32 位 trace ID，并写入 `messages.langfuse_trace_id`。
- graph config 保留 checkpoint `thread_id`，同时注入请求级 CallbackHandler、`tgpp-agent` run name 和 session/user/run/message/environment metadata。
- SSE 收尾同时识别默认 `LangGraph` 与注入 run name 后的 `tgpp-agent` 顶层 `on_chain_end`，保证 traced run 正常持久化 final state。
- pause/resume 复用同一 trace ID、使用新 handler；失败和取消保留入口生成的 trace ID；fork 不复制运行标识。
- Langfuse client 进程级复用，handler 请求级隔离；缺 key、显式关闭或 SDK 初始化失败均不影响聊天结果。
- 默认不上传问题、答案、历史和检索正文。候选块只留定位/评分字段与字符数，敏感键始终遮蔽，任意 payload 有长度、数量和递归深度边界。
- 部署脚本自动把当前 git 短 SHA 注入 `LANGFUSE_RELEASE`。

## 验证记录

- `make lint`：通过（backend Ruff/Black/MyPy；ingestion Ruff/Black）。
- `make test`：435 unit 通过；136 integration 通过、1 跳过。
- Agent 真实依赖子集：全量 integration 中 simple 5 题 + complex 5 题通过。
- `make eval`：1 个 canned smoke 通过、2 个 live suite 按环境门禁跳过。
- `make check-openapi-diff`：通过，2 个既有前端聚合类型 warning。
- Langfuse SDK contract：真实 v4 callback + 内存 exporter 验证 root/node 父子关系、实际分支、同名 retry、异常/取消 observation 收尾。
- Langfuse Cloud 冒烟：真实项目写入无 LLM 的两节点 graph；确认 `tgpp-agent → classify/retrieve`、trace/session/user metadata 可查询；原始问题、指代消解、改写查询、self-RAG missing、答案和 chunk 正文均被遮蔽，二维候选池仍保留 chunk ID/定位/评分诊断字段。

## 运维开关

- 紧急关闭：`LANGFUSE_TRACING_ENABLED=false` 后重启 API。
- 降采样：调整 `LANGFUSE_SAMPLE_RATE`（范围 `0.0-1.0`）后重启 API。
- 内容采集：生产保持 `LANGFUSE_CAPTURE_CONTENT=false`；只有完成隐私评审的环境才允许打开。
- Cloud 不可达：主链路 fail-open；看 API 日志中的 Langfuse exporter warning，恢复后重启可重建 exporter。

## PR2 边界

当前节点 span 能看节点顺序和耗时，但自定义 `LiteLLMClient` 不会自动生成 generation observations。README 已改为如实描述；token stream、usage/cost、TTFT、embedding/rerank 子观测在 Issue #9 PR2 实现。
