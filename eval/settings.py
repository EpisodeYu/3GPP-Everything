"""eval 子项目运行时配置（读 .env / 自动 host 解析）。

设计要点：
- 顶层 `.env` 中 LITELLM / Qdrant 都填 `host.docker.internal:<port>`，给 Docker compose
  里的 backend / ingestion 用。
- eval 子项目通常在 host shell 跑（`cd eval && uv run ...`）；host 上没 docker.internal
  解析，自动把 `host.docker.internal` 替换为 `localhost`。
- 兼容 `EVAL_*` 前缀显式覆盖（CI / 远程跑时用）。

关键环境变量：
    LITELLM_BASE_URL / LITELLM_API_KEY
    QDRANT_URL / QDRANT_API_KEY / QDRANT_COLLECTION_PREFIX
    VOYAGE_EMBEDDING_MODEL（默认 voyage-4-large）
    LLM_AGENT_MODEL（默认 mimo-v2.5-pro，用于 T2 LLM 转化）
    LLM_LIGHT_MODEL（默认 mimo-v2.5）
    EMBEDDING_PROVIDER（默认 voyage）

读取顺序（pydantic-settings 自然实现）：
    1. process env
    2. /home/s1yu/3GPP-Everything/.env（顶层）
"""

from __future__ import annotations

import socket
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def _resolve_docker_internal_host(url: str) -> str:
    """若 url 含 `host.docker.internal` 且当前 host 解析失败 → 替换为 `localhost`。

    项目里 ingestion/scripts/m2_poc_verify.py / m3_chunk_id_drift.py 都是这个套路；
    集中到 settings 里避免重复。
    """
    if "host.docker.internal" not in url:
        return url
    try:
        socket.gethostbyname("host.docker.internal")
        return url
    except OSError:
        return url.replace("host.docker.internal", "localhost")


class EvalSettings(BaseSettings):
    """eval 子项目运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_FILE) if DEFAULT_ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    litellm_base_url: str = Field(default="http://localhost:4000/v1")
    litellm_api_key: str = Field(default="")

    qdrant_url: str = Field(default="http://localhost:6333")
    qdrant_api_key: str = Field(default="")
    qdrant_collection_prefix: str = Field(default="tgpp_chunks")

    embedding_provider: str = Field(default="voyage")
    voyage_embedding_model: str = Field(default="voyage-4-large")

    # M3 LLM 用途：
    # - T2 转化：mimo-v2.5-pro（用户显式选择，700M token 余量）
    # - T6 旁证 MCQ runner：mimo-v2.5 + glm-5.1 各跑
    llm_agent_model: str = Field(default="mimo-v2.5-pro")
    llm_light_model: str = Field(default="mimo-v2.5")
    llm_judge_model: str = Field(default="glm-5.1")

    # M7.3 Langfuse Dataset / Trace 上报；缺任一 key 即 disable
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")
    langfuse_host: str = Field(default="https://cloud.langfuse.com")

    @property
    def resolved_litellm_base_url(self) -> str:
        return _resolve_docker_internal_host(self.litellm_base_url)

    @property
    def resolved_qdrant_url(self) -> str:
        return _resolve_docker_internal_host(self.qdrant_url)

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key.strip() and self.langfuse_secret_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> EvalSettings:
    return EvalSettings()


__all__ = [
    "DEFAULT_ENV_FILE",
    "REPO_ROOT",
    "EvalSettings",
    "get_settings",
]
