# LiteLLM proxy（standalone 自托管）

本目录给 `deploy/docker-compose.standalone.yml` 的 `litellm` 服务提供配置。
项目所有 LLM / embedding / rerank 调用都经过这个 OpenAI 兼容 proxy 转发到上游——
**切换模型/供应商只改这里，业务代码（backend / ingestion）一行不动**。

> dev / prod compose 不用本目录：它们假设宿主已运行 LiteLLM（maintainer 私有环境）。
> 外部用户自托管请走 standalone，本目录即其配置来源。

## 三个文件、两处必须对齐

| 文件 | 作用 |
|------|------|
| `config.yaml`（cp 自 `.example`） | model_name → 上游模型 的映射表 |
| `.env`（cp 自 `.example`） | 上游 API key + proxy 的 master key |
| 项目根 `.env` 的 `LITELLM_API_KEY` | 业务访问本 proxy 用的 Bearer token |

**两处耦合，错一个就 401 / 模型找不到：**

1. **proxy 鉴权**：项目根 `.env` 的 `LITELLM_API_KEY` **必须等于** 本目录 `.env` 的
   `LITELLM_MASTER_KEY`。前者是 client 出示的 token，后者是 proxy 校验的密钥。
2. **模型名**：`config.yaml` 里的 `model_name` **必须覆盖** 项目根 `.env` 里
   `LLM_AGENT_MODEL` / `LLM_LIGHT_MODEL` / `LLM_VISION_MODEL` /
   `VOYAGE_EMBEDDING_MODEL` / `VOYAGE_RERANK_MODEL` 引用的每一个名字。

## 快速配置

```bash
cd deploy/litellm
cp config.yaml.example config.yaml
cp .env.example .env
# 编辑 .env：填 LITELLM_MASTER_KEY（= 项目根 .env 的 LITELLM_API_KEY）+ 上游 key
# 编辑 config.yaml：选国产栈（默认）或取消注释 OpenAI 栈
```

`config.yaml` 自带两套 stack：

- **栈 A 国产栈**（默认启用）：`mimo-v2.5-pro` / `mimo-v2.5` + Voyage。与项目 `.env`
  默认值对齐，**开箱不用改 `.env` 的模型名**，只需填 MiMo + Voyage key。
- **栈 B OpenAI 栈**：取消 `config.yaml` 里对应注释，并把项目根 `.env` 的
  `LLM_AGENT_MODEL=gpt-4o` 等改成 OpenAI 模型名，填 `OPENAI_API_KEY`。

## 验证

`litellm` 容器起来后，`/ready` 的 litellm 探针会打 `http://litellm:4000/health/liveliness`。
proxy 进程起来即绿——liveliness **不校验上游 key**，真正推理时才需要有效 key。

```bash
curl 127.0.0.1:8002/ready    # litellm 项应为 ok
```

> `config.yaml` 与 `.env`（真实值）已被 `.gitignore` 忽略，不会误提交。
