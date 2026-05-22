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


def test_retrieval_defaults_match_m75_calibration() -> None:
    """M7.5 retrieval 校准选定的默认值：dense/sparse 50、final_top_n 80、rerank_top_k 5。

    详见 `docs/04-handoff/2026-05-22-m7.5-complete.md §3.2` +
    `eval-results/m7-rerank-ablation.md`。改这些默认值必须先跑 ablation
    更新报告，否则会破坏 D13 第一档评测对照口径。
    """
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.RETRIEVAL_DENSE_TOP_K == 50
    assert s.RETRIEVAL_SPARSE_TOP_K == 50
    assert s.RETRIEVAL_RRF_K == 60
    assert s.RETRIEVAL_FINAL_TOP_K == 80
    assert s.RERANK_TOP_K == 5


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


def test_allowed_origins_csv_string(monkeypatch) -> None:
    """运维 .env 习惯用逗号分隔，必须能解析。"""
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8082,http://127.0.0.1:8082")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.ALLOWED_ORIGINS == ["http://localhost:8082", "http://127.0.0.1:8082"]


def test_allowed_origins_single_value() -> None:
    s = Settings(_env_file=None, ALLOWED_ORIGINS="https://tgpp.example.com")  # type: ignore[call-arg]
    assert s.ALLOWED_ORIGINS == ["https://tgpp.example.com"]


def test_allowed_origins_json_array(monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://a.com", "https://b.com"]')
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.ALLOWED_ORIGINS == ["https://a.com", "https://b.com"]


def test_allowed_origins_empty_string() -> None:
    s = Settings(_env_file=None, ALLOWED_ORIGINS="")  # type: ignore[call-arg]
    assert s.ALLOWED_ORIGINS == []
