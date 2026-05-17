"""Settings 字段读取 / 默认值 / 派生属性。"""

from __future__ import annotations

from app.core.config import Settings


def test_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.APP_ENV == "dev"
    assert s.EMBEDDING_PROVIDER == "voyage"
    assert s.EMBEDDING_DIMENSIONS == 1024
    assert s.QDRANT_COLLECTION_PREFIX == "tgpp_chunks"
    assert s.qdrant_collection == "tgpp_chunks_voyage_d1024"
    assert s.bm25_dir == "/data/tgpp/bm25/voyage"


def test_sync_url_strips_asyncpg() -> None:
    s = Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://u:p@h/db")  # type: ignore[call-arg]
    assert s.database_url_sync == "postgresql://u:p@h/db"


def test_secret_str_does_not_leak_in_repr() -> None:
    s = Settings(_env_file=None, APP_SECRET_KEY="topsecret")  # type: ignore[call-arg]
    assert "topsecret" not in repr(s)
    assert s.APP_SECRET_KEY.get_secret_value() == "topsecret"


def test_env_var_override(monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "2048")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "glm")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.EMBEDDING_DIMENSIONS == 2048
    assert s.qdrant_collection == "tgpp_chunks_glm_d2048"
