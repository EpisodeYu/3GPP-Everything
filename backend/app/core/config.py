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
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _find_env_file() -> tuple[str, ...]:
    """从当前 cwd 向上找 `.env`，优先 cwd 内、其次项目根。

    pytest 经常在 `backend/` 启动；运维 / IDE 可能在项目根启动。把候选路径都给
    pydantic-settings，它会按顺序加载（后面的覆盖前面的）。
    """
    candidates: list[str] = [".env"]
    here = Path(__file__).resolve()
    for parent in here.parents[:6]:  # backend/app/core -> root 不会超过 6 级
        env_path = parent / ".env"
        rel_or_abs = str(env_path)
        if rel_or_abs not in candidates:
            candidates.append(rel_or_abs)
    return tuple(candidates)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_find_env_file(),
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

    # === Postgres（2026-05-27 解耦：默认指向 compose 内 tgpp-postgres；host 直跑 uv 时
    # 改 127.0.0.1:55432，见 deploy/docker-compose.yml dev 端口映射）===
    DATABASE_URL: str = "postgresql+asyncpg://tgpp_app:CHANGEME@tgpp-postgres:5432/tgpp_everything"

    # === Redis（2026-05-27 解耦：默认指向 compose 内 tgpp-redis，db=0；
    # host 直跑改 127.0.0.1:56379）===
    REDIS_URL: str = "redis://:CHANGEME@tgpp-redis:6379/0"

    # === Langfuse ===
    LANGFUSE_PUBLIC_KEY: SecretStr = SecretStr("")
    LANGFUSE_SECRET_KEY: SecretStr = SecretStr("")
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # === 鉴权 ===
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    BOOTSTRAP_ADMIN_INVITE_CODE: SecretStr = SecretStr("")
    # NoDecode 关掉 pydantic-settings 默认的 JSON 解析（默认会把 list 字段当 JSON
    # 解，运维 .env 写 CSV 直接抛 JSONDecodeError）。下面 field_validator 兜底处理
    # CSV / 单 origin / JSON 三种写法。
    ALLOWED_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def _split_allowed_origins(cls, v: Any) -> Any:
        """允许 `.env` 写 CSV 或 JSON list 或单 origin；空字符串视作空 list。

        历史背景：pydantic-settings v2 默认对 list 字段走 JSON 解析，导致 `.env`
        里的 `https://a.com,https://b.com` 直接报错。`NoDecode` 关掉默认 JSON
        decode 后，本 validator 把字符串归一化为 `list[str]`：
        - JSON array (`["a","b"]`) → json.loads
        - CSV (`a,b`) → split + strip
        - 单值 (`a`) → `[a]`
        - 空 / None → `[]`
        """
        if v is None:
            return []
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            if s.startswith("["):
                import json

                return json.loads(s)
            return [item.strip() for item in s.split(",") if item.strip()]
        return v

    # === ingestion 数据目录（retrieval/sparse 需要读 BM25 jsonl）===
    INGEST_DATA_DIR: str = "/data/tgpp"

    # === 检索默认参数 ===
    # 2026-05-22 M7.5 校准（详见 docs/04-handoff/2026-05-22-m7.5-complete.md §3.2
    # + eval-results/m7-rerank-ablation.md）：
    # - dense / sparse top_k 30→50：给 rerank 更宽的 candidate pool；
    #   section_recall@5 75%→80%，spec_recall@5 85%→92.5%（hand_crafted 40 题）
    # - final_top_n 50→80：与 dense/sparse top_k=50 + RRF 去重容量匹配
    # - RRF k 保持 60：实测 30/60/100 在 rerank 下游被 rerank 完全洗掉，无差异
    # - RERANK_TOP_K 保持 5：rerank top_k=10 会让 generate prompt context 翻倍
    #   （额外 token 成本 + latency），而 D13 主硬指标只看 section@5
    # 改动 latency 影响：dense/sparse 每个查询多 ~20 chunks 但 qdrant/bm25 都是 O(log N)
    # 走索引，实测 p50 605 vs 旧 610 ms 持平；总仍 < 800ms 预算
    RETRIEVAL_DENSE_TOP_K: int = 50
    RETRIEVAL_SPARSE_TOP_K: int = 50
    RETRIEVAL_RRF_K: int = 60
    RETRIEVAL_FINAL_TOP_K: int = 80
    RERANK_TOP_K: int = 5
    RETRIEVAL_CACHE_TTL_S: int = 3600

    # === 检索缓存 key 前缀 ===
    CACHE_KEY_PREFIX: str = "tgpp:cache"

    # === 成本告警（M7.4，Q2 决策：仅 log warning，不接 webhook）===
    # 日 / 月美元阈值；超过对应阈值时 alerts daily job 仅写 structlog warning。
    # 0 / 负值视作 disabled（不告警）；调度时区沿用 APP_TIMEZONE。
    ALERT_DAILY_USD: float = 5.0
    ALERT_DAILY_USD_CRITICAL: float = 10.0
    ALERT_MONTHLY_USD: float = 50.0
    # 每日聚合 job 的本地时刻（小时 0-23），默认凌晨 1 点，避免与 daily eval 重叠。
    ALERT_DAILY_AGGREGATE_HOUR: int = 1
    # 显式开关：测试 / 单进程 worker 部署可关掉 scheduler；缺省走 lifespan 自动启动。
    ALERT_SCHEDULER_ENABLED: bool = True

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
