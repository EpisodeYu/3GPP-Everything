"""tgpp-eval: M3 评测脚手架。

子模块约定（docs/03-development/06-evaluation-and-observability.md）：

- `retrieval/`  — retrieval-only 评测（M3 维度决胜的核心）
- `teleqna/`   — TeleQnA 拉取、过滤（按 17 篇 whitelist 硬约束）
- `builder/`   — MCQ → 开放问答 LLM 转化 + 人审 CLI（M3 中段引入）
- `golden/`    — 人审通过后的 v1.yaml（最终交付）
- `scripts/`   — 一次性脚本 / spike 验证

运行约定：

- 顶层项目用 `host.docker.internal` 指本机服务（在 Docker compose 内）；
  本子项目运行在 host shell（uv run），自动把 `host.docker.internal`
  替换为 `localhost`。详见 `eval.settings`。
- 不直连厂商 SDK；embedding 走 LiteLLM proxy 的 OpenAI 兼容端点。
- M3 阶段不接 Langfuse；输出落地到 `eval-results/m3-embedding-poc/`。
"""

__version__ = "0.1.0"
