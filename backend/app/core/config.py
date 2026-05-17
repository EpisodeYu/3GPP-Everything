"""统一配置入口（pydantic-settings v2）。

字段与 `.env.example` 一一映射；新增 key 必须同步改 `.env.example` 与
`docs/03-development/01-infrastructure.md §2.4` 的清单（CLAUDE.md §8）。

约定：
- 所有 secret 字段在日志/异常里 **不** 输出原值；只输出存在性（bool）
- `get_settings()` 用 lru_cache，单进程内单例
- 测试覆盖（test_config.py）只校验"字段读取 + alias + 默认值"，不连真实服务
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # === 环境 ===
    APP_ENV: Literal["dev", "prod"] = "dev"
    APP_DEBUG: bool = True
    APP_TIMEZONE: str = "Asia/Shanghai"
    APP_SECRET_KEY: SecretStr = SecretStr("")

    # === API ===
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8002

    # === LiteLLM proxy ===
    LITELLM_BASE_URL: str = "http://host.docker.internal:4000/v1"
    LITELLM_API_KEY: SecretStr = SecretStr("")
    LITELLM_TIMEOUT_S: float = 120.0

    # 模型 (与 LiteLLM config.yaml 对齐)
    LLM_AGENT_MODEL: str = "mimo-v2.5-pro"
    LLM_LIGHT_MODEL: str = "mimo-v2.5"
    LLM_VISION_MODEL: str = "mimo-v2.5"

    # === Embedding / Rerank ===
    EMBEDDING_PROVIDER: Literal["voyage", "glm"] = "voyage"
    VOYAGE_EMBEDDING_MODEL: str = "voyage-4-large"
    VOYAGE_RERANK_MODEL: str = "rerank-2.5"
    GLM_EMBEDDING_MODEL: str = "embedding-3"
    EMBEDDING_DIMENSIONS: int = 1024
    VOYAGE_OUTPUT_DIMENSION: int = 1024

    # === Tavily ===
    TAVILY_API_KEY: SecretStr = SecretStr("")

    # === Qdrant ===
    QDRANT_URL: str = "http://host.docker.internal:6333"
    QDRANT_API_KEY: SecretStr = SecretStr("")
    QDRANT_COLLECTION_PREFIX: str = "tgpp_chunks"

    # === Postgres ===
    DATABASE_URL: str = (
        "postgresql+asyncpg://tgpp_app:CHANGEME@host.docker.internal:5432/tgpp_everything"
    )

    # === Redis ===
    REDIS_URL: str = "redis://host.docker.internal:6379/5"

    # === Langfuse ===
    LANGFUSE_PUBLIC_KEY: SecretStr = SecretStr("")
    LANGFUSE_SECRET_KEY: SecretStr = SecretStr("")
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # === 鉴权 ===
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    BOOTSTRAP_ADMIN_INVITE_CODE: SecretStr = SecretStr("")
    ALLOWED_ORIGINS: list[str] = Field(default_factory=list)

    # === ingestion 数据目录（retrieval/sparse 需要读 BM25 jsonl）===
    INGEST_DATA_DIR: str = "/data/tgpp"

    # === 检索默认参数 ===
    RETRIEVAL_DENSE_TOP_K: int = 30
    RETRIEVAL_SPARSE_TOP_K: int = 30
    RETRIEVAL_RRF_K: int = 60
    RETRIEVAL_FINAL_TOP_K: int = 50
    RERANK_TOP_K: int = 5
    RETRIEVAL_CACHE_TTL_S: int = 3600

    # === 检索缓存 key 前缀 ===
    CACHE_KEY_PREFIX: str = "tgpp:cache"

    @property
    def database_url_sync(self) -> str:
        """同步 driver URL（alembic / sqlite 单测用），把 +asyncpg 换成默认 psycopg。"""
        return self.DATABASE_URL.replace("+asyncpg", "")

    @property
    def qdrant_collection(self) -> str:
        """主 collection 名：{prefix}_{provider}_d{dim}。"""
        return (
            f"{self.QDRANT_COLLECTION_PREFIX}_{self.EMBEDDING_PROVIDER}"
            f"_d{self.EMBEDDING_DIMENSIONS}"
        )

    @property
    def bm25_dir(self) -> str:
        """BM25 持久化目录 `{INGEST_DATA_DIR}/bm25/{provider}/`。"""
        return f"{self.INGEST_DATA_DIR}/bm25/{self.EMBEDDING_PROVIDER}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
