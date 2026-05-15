"""Vision pipeline 单测。

覆盖：
- prompts.parse_vision_json：直 JSON / 围栏 / 截块 / 失败
- prompts.normalize_vision_payload：缺字段 / 非法 figure_kind / description 空
- VisionResolver:
    - cache hit
    - cache miss → mimo 调成功 → 写缓存
    - JSON 解析失败 → 重试 → dead-letter
    - HTTP 异常 → 重试 → dead-letter
    - dead-letter 已写 → 不再调
    - 图片读失败 → 返回 None
    - finish_reason=length → 计入失败重试
"""

from __future__ import annotations

import fakeredis
import pytest

from ingestion.images.prompts import normalize_vision_payload, parse_vision_json
from ingestion.images.vision import (
    CACHE_KEY_DEAD,
    CACHE_KEY_OK,
    CACHE_KEY_RETRY,
    VisionResolver,
    _LiteLLMClient,
    _VisionCache,
)

# ============== prompts.parse_vision_json ==============


def test_parse_json_plain() -> None:
    txt = '{"figure_kind": "logo", "description": "a logo"}'
    assert parse_vision_json(txt) == {"figure_kind": "logo", "description": "a logo"}


def test_parse_json_with_fence() -> None:
    txt = '```json\n{"figure_kind": "logo", "description": "a logo"}\n```'
    assert parse_vision_json(txt) == {"figure_kind": "logo", "description": "a logo"}


def test_parse_json_with_prose_around() -> None:
    txt = 'sure, here:\n{"figure_kind": "logo", "description": "x"}\nbye'
    assert parse_vision_json(txt) == {"figure_kind": "logo", "description": "x"}


def test_parse_json_failure() -> None:
    assert parse_vision_json("not json at all") is None
    assert parse_vision_json("") is None


# ============== prompts.normalize_vision_payload ==============


def test_normalize_full_payload() -> None:
    out = normalize_vision_payload(
        {
            "figure_kind": "ARCHITECTURE",
            "visible_labels": ["UE", "AMF", " "],
            "visible_acronyms": ["NR", 1, "AMF"],
            "description": "  arch diagram  ",
            "spec_role": "  reference architecture  ",
            "undescribable_reason": "",
        }
    )
    assert out is not None
    assert out["figure_kind"] == "architecture"
    assert out["visible_labels"] == ["UE", "AMF"]
    assert out["visible_acronyms"] == ["NR", "1", "AMF"]
    assert out["description"] == "arch diagram"
    assert out["spec_role"] == "reference architecture"


def test_normalize_invalid_figure_kind_falls_back_to_other() -> None:
    out = normalize_vision_payload({"figure_kind": "weirdkind", "description": "x"})
    assert out is not None
    assert out["figure_kind"] == "other"


def test_normalize_empty_description_returns_none() -> None:
    assert normalize_vision_payload({"figure_kind": "logo", "description": "  "}) is None
    assert normalize_vision_payload({}) is None


def test_normalize_missing_fields_get_defaults() -> None:
    out = normalize_vision_payload({"description": "ok"})
    assert out is not None
    assert out["figure_kind"] == "other"
    assert out["visible_labels"] == []
    assert out["visible_acronyms"] == []
    assert out["spec_role"] == ""


# ============== _VisionCache ==============


def _fake_cache() -> _VisionCache:
    client = fakeredis.FakeRedis(decode_responses=True)
    return _VisionCache(redis_client=client)


def test_cache_set_get_roundtrip() -> None:
    cache = _fake_cache()
    payload = {"description": "x", "figure_kind": "logo"}
    cache.set("abc", payload)
    assert cache.get("abc") == payload


def test_cache_bump_retry_increments() -> None:
    cache = _fake_cache()
    assert cache.get_retry_count("h") == 0
    assert cache.bump_retry("h", error="boom", ctx={"spec_id": "23.501"}) == 1
    assert cache.get_retry_count("h") == 1
    assert cache.bump_retry("h", error="boom2", ctx={}) == 2
    assert cache.get_retry_count("h") == 2


def test_cache_set_clears_retry() -> None:
    cache = _fake_cache()
    cache.bump_retry("h", error="boom", ctx={})
    assert cache.get_retry_count("h") == 1
    cache.set("h", {"description": "x"})
    assert cache.get_retry_count("h") == 0


def test_cache_move_to_dead_marks_image() -> None:
    cache = _fake_cache()
    assert not cache.is_dead("h")
    cache.move_to_dead("h", error="dead", ctx={"spec_id": "x"})
    assert cache.is_dead("h")


def test_cache_disabled_when_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    cache = _VisionCache()
    assert cache.enabled is False
    cache.set("h", {"x": 1})
    assert cache.get("h") is None
    assert cache.bump_retry("h", error="e", ctx={}) == 1


# ============== VisionResolver ==============


class _StubHttp(_LiteLLMClient):
    """LiteLLMClient 的可控替身：按 fixture 队列吐 payload，或抛异常。"""

    def __init__(self, *, responses: list[object]) -> None:
        # 不调父类 __init__ —— 不需要真 httpx 客户端
        self.base_url = "http://stub"
        self.api_key = "stub"
        self._owns_client = False
        self._client = None  # type: ignore[assignment]
        self._responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, body: dict) -> dict:  # type: ignore[override]
        self.calls.append(body)
        if not self._responses:
            raise AssertionError("StubHttp out of responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]

    def close(self) -> None:
        return None


def _ok_payload(text: str, *, completion_tokens: int = 100, model: str = "mimo-v2.5") -> dict:
    return {
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "completion_tokens": completion_tokens,
            "completion_tokens_details": {"reasoning_tokens": 50},
        },
    }


def _length_truncated_payload() -> dict:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "length",
            }
        ],
        "usage": {
            "completion_tokens": 8192,
            "completion_tokens_details": {"reasoning_tokens": 8192},
        },
    }


def _fixed_loader(*, image_bytes: bytes = b"\x89PNG\r\n\x1a\n", sha256: str = "h1") -> object:
    def _loader(image_path: str) -> tuple[bytes, str]:
        return image_bytes, sha256

    return _loader


def _ctx() -> dict:
    return {
        "spec_id": "23.501",
        "clause": "4.2.3",
        "section_title": "Non-roaming reference architecture",
        "image_alt": "alt",
        "spec_caption": "Figure 4.2.3-1: ...",
        "gsma_caption_text": "should be pruned",
        "surrounding_paragraph": "should be pruned",
    }


def test_resolver_calls_mimo_and_caches_on_success() -> None:
    text = (
        '{"figure_kind": "architecture", '
        '"visible_labels": ["UE","AMF"], '
        '"visible_acronyms": ["UE","AMF"], '
        '"description": "arch diagram", '
        '"spec_role": "reference architecture", '
        '"undescribable_reason": ""}'
    )
    http = _StubHttp(responses=[_ok_payload(text)])
    cache = _fake_cache()
    resolver = VisionResolver(
        http_client=http,
        cache=cache,
        image_loader=_fixed_loader(),
        model="mimo-v2.5",
    )

    out = resolver("img.jpg", _ctx())
    assert out is not None
    assert out["description"] == "arch diagram"
    assert out["figure_kind"] == "architecture"
    assert out["visible_labels"] == ["UE", "AMF"]
    assert out["cached"] is False
    assert out["completion_tokens"] == 100
    assert out["reasoning_tokens"] == 50
    assert len(http.calls) == 1

    # 缓存命中：不再调 http
    out2 = resolver("img.jpg", _ctx())
    assert out2 is not None
    assert out2["cached"] is True
    assert out2["description"] == "arch diagram"
    assert len(http.calls) == 1  # 没新增

    # ctx 进缓存的 retry queue 时已 prune（用一次失败触发）
    # 这里通过直接 inspect Redis：成功路径不应有 retry 记录
    fake = cache._client  # type: ignore[attr-defined]
    assert fake.get(CACHE_KEY_RETRY.format(sha256="h1")) is None
    assert fake.get(CACHE_KEY_OK.format(sha256="h1")) is not None


def test_resolver_retries_on_json_failure_then_succeeds() -> None:
    bad = _ok_payload("not json at all")
    good_text = '{"figure_kind": "logo", "description": "lg"}'
    good = _ok_payload(good_text)
    http = _StubHttp(responses=[bad, good])
    resolver = VisionResolver(
        http_client=http,
        cache=_fake_cache(),
        image_loader=_fixed_loader(),
        model="m",
        max_retries=3,
    )
    out = resolver("img.jpg", _ctx())
    assert out is not None
    assert out["description"] == "lg"
    assert len(http.calls) == 2


def test_resolver_dead_letter_after_max_retries() -> None:
    bad = _ok_payload("garbage that wont parse")
    http = _StubHttp(responses=[bad, bad, bad, bad])  # 共 max_retries+1 = 4 次
    cache = _fake_cache()
    dead_calls: list[tuple[str, dict, str]] = []
    resolver = VisionResolver(
        http_client=http,
        cache=cache,
        image_loader=_fixed_loader(sha256="dead-h"),
        model="m",
        max_retries=3,
        on_dead_letter=lambda p, c, e: dead_calls.append((p, c, e)),
    )
    out = resolver("img.jpg", _ctx())
    assert out is None
    assert len(http.calls) == 4
    assert dead_calls and dead_calls[0][0] == "img.jpg"
    assert cache.is_dead("dead-h")
    # retry 队列应已被 move_to_dead 清掉
    fake = cache._client  # type: ignore[attr-defined]
    assert fake.get(CACHE_KEY_RETRY.format(sha256="dead-h")) is None
    assert fake.get(CACHE_KEY_DEAD.format(sha256="dead-h")) is not None


def test_resolver_dead_letter_on_http_errors() -> None:
    http = _StubHttp(
        responses=[
            RuntimeError("net1"),
            RuntimeError("net2"),
            RuntimeError("net3"),
            RuntimeError("net4"),
        ]
    )
    cache = _fake_cache()
    resolver = VisionResolver(
        http_client=http,
        cache=cache,
        image_loader=_fixed_loader(sha256="net-h"),
        model="m",
        max_retries=3,
    )
    out = resolver("img.jpg", _ctx())
    assert out is None
    assert cache.is_dead("net-h")
    assert len(http.calls) == 4


def test_resolver_skips_known_dead_letter_image() -> None:
    cache = _fake_cache()
    cache.move_to_dead("dead-h", error="prev", ctx={})
    http = _StubHttp(responses=[])  # 一次都不应被调用
    resolver = VisionResolver(
        http_client=http,
        cache=cache,
        image_loader=_fixed_loader(sha256="dead-h"),
        model="m",
    )
    out = resolver("img.jpg", _ctx())
    assert out is None
    assert http.calls == []


def test_resolver_returns_none_when_image_load_fails() -> None:
    def _bad_loader(p: str) -> tuple[bytes, str]:
        raise FileNotFoundError("no such image")

    http = _StubHttp(responses=[])  # 一次都不应被调用
    resolver = VisionResolver(
        http_client=http,
        cache=_fake_cache(),
        image_loader=_bad_loader,
        model="m",
    )
    assert resolver("missing.jpg", _ctx()) is None
    assert http.calls == []


def test_resolver_truncated_response_treated_as_failure() -> None:
    # 第 1 次 length 截断 → 第 2 次成功
    http = _StubHttp(
        responses=[
            _length_truncated_payload(),
            _ok_payload('{"figure_kind": "logo", "description": "ok"}'),
        ]
    )
    resolver = VisionResolver(
        http_client=http,
        cache=_fake_cache(),
        image_loader=_fixed_loader(),
        model="m",
        max_retries=3,
    )
    out = resolver("img.jpg", _ctx())
    assert out is not None
    assert out["description"] == "ok"
    assert len(http.calls) == 2


def test_resolver_returns_undescribable_after_exhausting_retries() -> None:
    """mimo 连续 4 次都返回 undescribable → 接受最终结果并缓存（不进 dead-letter）。

    见 POC handoff §6.3：mimo-v2.5 偶发把可见图片误判为 undescribable，
    增加额外 retry 防止单次误判即写入降级结果；全部仍 undescribable 才认账。
    """
    text = (
        '{"figure_kind": "undescribable", "description": "blank scan", '
        '"undescribable_reason": "image is blank"}'
    )
    http = _StubHttp(responses=[_ok_payload(text) for _ in range(4)])
    cache = _fake_cache()
    resolver = VisionResolver(
        http_client=http,
        cache=cache,
        image_loader=_fixed_loader(sha256="u-h"),
        model="m",
        undescribable_retries=3,
    )
    out = resolver("img.jpg", _ctx())
    assert out is not None
    assert out["figure_kind"] == "undescribable"
    assert out["undescribable_reason"] == "image is blank"
    # 初始 1 次 + 3 次 retry = 4 次总调用
    assert len(http.calls) == 4
    # undescribable 仍写正常缓存（不会反复调）
    assert cache.get("u-h") is not None
    assert not cache.is_dead("u-h")


def test_resolver_retries_undescribable_and_recovers_with_valid_kind() -> None:
    """mimo 首次误判 undescribable，retry 后返回真实 figure_kind → 用真实结果。"""
    undescribable = _ok_payload(
        '{"figure_kind": "undescribable", "description": "blank?", '
        '"undescribable_reason": "uncertain"}'
    )
    good = _ok_payload(
        '{"figure_kind": "message_flow", "description": "UE -> AMF call flow", '
        '"undescribable_reason": ""}'
    )
    http = _StubHttp(responses=[undescribable, good])
    cache = _fake_cache()
    resolver = VisionResolver(
        http_client=http,
        cache=cache,
        image_loader=_fixed_loader(sha256="u-h2"),
        model="m",
        undescribable_retries=3,
    )
    out = resolver("img.jpg", _ctx())
    assert out is not None
    assert out["figure_kind"] == "message_flow"
    assert out["description"] == "UE -> AMF call flow"
    assert len(http.calls) == 2
    # 缓存的是好结果
    cached = cache.get("u-h2")
    assert cached is not None
    assert cached["figure_kind"] == "message_flow"


def test_resolver_undescribable_disabled_when_retries_zero() -> None:
    """undescribable_retries=0 时保持旧行为：单次 undescribable 立即接受。"""
    text = (
        '{"figure_kind": "undescribable", "description": "blank", '
        '"undescribable_reason": "blank"}'
    )
    http = _StubHttp(responses=[_ok_payload(text)])
    resolver = VisionResolver(
        http_client=http,
        cache=_fake_cache(),
        image_loader=_fixed_loader(sha256="u-h3"),
        model="m",
        undescribable_retries=0,
    )
    out = resolver("img.jpg", _ctx())
    assert out is not None
    assert out["figure_kind"] == "undescribable"
    assert len(http.calls) == 1


def test_resolver_prunes_long_ctx_from_retry_record() -> None:
    bad = _ok_payload("garbage")
    http = _StubHttp(responses=[bad, bad, bad, bad])
    cache = _fake_cache()
    resolver = VisionResolver(
        http_client=http,
        cache=cache,
        image_loader=_fixed_loader(sha256="prune-h"),
        model="m",
        max_retries=3,
    )
    ctx = _ctx()  # 含 surrounding_paragraph / gsma_caption_text
    out = resolver("img.jpg", ctx)
    assert out is None
    fake = cache._client  # type: ignore[attr-defined]
    import json as _json

    dead = _json.loads(fake.get(CACHE_KEY_DEAD.format(sha256="prune-h")))
    pruned = dead["ctx"]
    assert "surrounding_paragraph" not in pruned
    assert "gsma_caption_text" not in pruned
    assert pruned.get("spec_id") == "23.501"
    assert pruned.get("clause") == "4.2.3"
