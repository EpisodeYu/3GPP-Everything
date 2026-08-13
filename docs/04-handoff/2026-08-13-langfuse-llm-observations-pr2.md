# Langfuse LiteLLM 子观测（Issue #9 PR2）交付记录

> 日期：2026-08-13
> 基线：PR1 `feat/issue-9-langfuse-node-trace`
> 范围：为自定义 async `LiteLLMClient` 增加节点级 child observations；不修改图拓扑、SSE 协议或数据库 schema。

## 行为契约

- `chat()` 创建 `litellm.chat` generation；`chat_stream()` 创建 `litellm.chat_stream` generation；`embed()` 创建 `litellm.embedding`；`rerank()` 创建 `litellm.rerank` span。
- Langfuse v4 callback 创建节点 span 时，LangGraph 的 async node task 不保证继承 callback 的 OpenTelemetry current context。实现从当前 RunnableConfig 的 node run ID 解析 callback observation，并显式传入 `trace_id + parent_span_id`。兼容逻辑集中在 `current_langgraph_trace_context()`，SDK 结构变化时 fail-closed 为“不记录”，不会创建 orphan trace。
- 只有 traced node parent 存在时才初始化/写入 Langfuse；无 callback、kill-switch 关闭或后台调用保持 PR2 前的请求、返回值和异常语义。
- generation/embedding 记录 model、受限 model parameters、HTTP status、retry count、provider usage，以及 provider cost 或 `app.llm.pricing` 的同口径 cost。
- streaming 只在 observation active 时请求 `stream_options.include_usage`；首个正文 token 设置 completion start time，正常 `[DONE]` 合并输出；HTTP error、timeout、`CancelledError`、`GeneratorExit`/显式 close 都结束 observation。usage 缺失时标记 missing，不伪造精确 token/cost。
- rerank 是 span（不是 generation）：provider/估算 token 与 estimated cost 放 metadata，继续复用现有 `ApiUsage` hook 作为业务账本。

## 隐私与体积

- 生产默认 `LANGFUSE_CAPTURE_CONTENT=false`：chat 只记录 message/role/字符数和输出字符数/finish reason。
- embedding 永不上传向量，只记录输入数、字符数、返回向量数和维度；即使允许内容采集，也只有 dev 环境可记录最多 10 条、每条最多 500 字符的预览。
- rerank 默认不上传 query/document 正文，只记录数量、字符数、top_n 和结果 index/score；dev preview 使用相同边界。
- error observation 只记录异常类型、HTTP status、retry count 和 cancelled 标记，不写上游响应 body。

## 验证记录

- 针对性单元/集成/真实 SDK 契约：83 项通过；另补 streaming timeout、任务取消、显式 close 和 tracing-disabled 请求不变回归。
- `make lint`：backend Ruff/Black/MyPy 与 ingestion Ruff/Black 全绿。
- `make test-unit`：457 passed。
- `make test-int`：137 passed、1 skipped；第一次运行发现并修复无 parent 调用提前初始化 Langfuse 导致的测试顺序污染，第二次完整运行全绿。
- `make eval`：1 passed、2 个 live suite 按门禁 skipped。
- 真实账号/Cloud 冒烟：5 次受控调用覆盖 chat、embedding、rerank、正常 stream 和主动关闭 stream；Cloud trace `d662b7ec6ebfb43ab305d57b498240b3` 回读 11 个 observations，其中 5 个为 LiteLLM 子观测。父节点、generation/embedding/span 类型、usage/cost、stream TTFT、`ERROR/cancelled` 和正文/向量/文档不落 Cloud 均通过。

## 运维与排障

- 紧急关闭仍使用 `LANGFUSE_TRACING_ENABLED=false`；降采样使用 `LANGFUSE_SAMPLE_RATE`。关闭或无 node parent 时不会额外请求 streaming usage chunk。
- Cloud cost 是可观测性副本，不替代 PostgreSQL `ApiUsage`。chat/embedding 优先 provider usage/cost，provider 未给 cost 时走本地 pricing；rerank 当前以 metadata 中的 provider/approximate 口径解释。
- 若升级 Langfuse/LangGraph，必须先跑 `test_litellm_observations_are_direct_children_of_executing_nodes`；它覆盖 callback run registry、并发 embedding parent 和真实 v4 OTLP attributes，是显式 parent 兼容层的升级门禁。
- exporter 更新/结束异常全部吞掉并记 debug log；模型调用结果优先，不能因 telemetry 失败改变 Agent 状态。
