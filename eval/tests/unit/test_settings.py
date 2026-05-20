"""eval/settings.py 测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from eval.settings import EvalSettings, _resolve_docker_internal_host


class TestResolveDockerInternalHost:
    def test_no_replacement_when_no_docker_internal(self) -> None:
        url = "http://localhost:6333"
        assert _resolve_docker_internal_host(url) == url

    def test_replaces_when_host_unresolvable(self) -> None:
        # gethostbyname 抛 OSError → 替换为 localhost
        with patch("eval.settings.socket.gethostbyname", side_effect=OSError):
            assert (
                _resolve_docker_internal_host("http://host.docker.internal:6333")
                == "http://localhost:6333"
            )
            assert (
                _resolve_docker_internal_host("http://host.docker.internal:4000/v1")
                == "http://localhost:4000/v1"
            )

    def test_keeps_url_when_host_resolvable(self) -> None:
        # gethostbyname 成功（在 docker 容器内场景） → 保持 URL
        with patch("eval.settings.socket.gethostbyname", return_value="172.17.0.1"):
            assert (
                _resolve_docker_internal_host("http://host.docker.internal:6333")
                == "http://host.docker.internal:6333"
            )


class TestEvalSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 防御：清空可能干扰 default 的 env
        for key in (
            "LITELLM_BASE_URL",
            "LITELLM_API_KEY",
            "QDRANT_URL",
            "QDRANT_COLLECTION_PREFIX",
        ):
            monkeypatch.delenv(key, raising=False)
        # 关 .env 加载
        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.qdrant_url == "http://localhost:6333"
        assert s.qdrant_collection_prefix == "tgpp_chunks"
        assert s.embedding_provider == "voyage"
        assert s.voyage_embedding_model == "voyage-4-large"
        assert s.llm_agent_model == "mimo-v2.5-pro"
        assert s.llm_judge_model == "glm-5.1"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LITELLM_BASE_URL", "http://example.com:9999/v1")
        monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
        monkeypatch.setenv("QDRANT_URL", "http://qd.example.com:6333")
        s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.litellm_base_url == "http://example.com:9999/v1"
        assert s.litellm_api_key == "sk-test"
        assert s.qdrant_url == "http://qd.example.com:6333"

    def test_resolved_docker_internal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LITELLM_BASE_URL", "http://host.docker.internal:4000/v1")
        monkeypatch.setenv("LITELLM_API_KEY", "sk-x")
        monkeypatch.setenv("QDRANT_URL", "http://host.docker.internal:6333")
        with patch("eval.settings.socket.gethostbyname", side_effect=OSError):
            s = EvalSettings(_env_file=None)  # type: ignore[call-arg]
            assert s.resolved_litellm_base_url == "http://localhost:4000/v1"
            assert s.resolved_qdrant_url == "http://localhost:6333"
