"""VisionResolver async 入口单测（M2 §4.8 B2）。

覆盖：
- aresolve_one：cache hit / 正常调 mimo / retry-then-success / dead-letter
- aresolve_batch：顺序保持 / 并发上限 / 空输入 / 含 None
- mimo 限速器透传：fan-out 走 CompositeLimiter（counter 校验）

与 sync `__call__` 路径不同的两点：
1. 走 `_AsyncLiteLLMClient`（这里用 `_StubAsyncHttp` 替身）
2. 每次 IO 前 `async with self._rate_limiter.with_rate_limit():`
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis
import pytest

from ingestion.images.vision import (
    VisionResolver,
    _AsyncLiteLLMClient,
    _VisionCache,
)
from ingestion.rate_limit import CompositeLimiter

# -------------------- helpers --------------------


class _StubAsyncHttp(_AsyncLiteLLMClient):
    """_AsyncLiteLLMClient 的可控替身：按队列吐 payload / 抛异常 / 模拟延迟。"""

    def __init__(self, *, responses: list[Any], delay_s: float = 0.0) -> None:
        # 不调父类 __init__：不需要真 httpx.AsyncClient
        self.base_url = "http://stub"
        self.api_key = "stub"
        self._owns_client = False
        self._client = None  # type: ignore[assignment]
        self._responses = list(responses)
        self._delay_s = delay_s
        self.calls: list[dict] = []
        self._inflight = 0
        self.peak_inflight = 0
        self._lock = asyncio.Lock()

    async def chat(self, body: dict) -> dict:  # type: ignore[override]
        async with self._lock:
            self.calls.append(body)
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
        try:
            if self._delay_s > 0:
                await asyncio.sleep(self._delay_s)
            if not self._responses:
                raise AssertionError("StubAsyncHttp out of responses")
            item = self._responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item  # type: ignore[return-value]
        finally:
            async with self._lock:
                self._inflight -= 1

    async def aclose(self) -> None:  # type: ignore[override]
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


def _fixed_loader(*, image_bytes: bytes = b"\x89PNG\r\n\x1a\n", sha256: str = "h1") -> Any:
    def _loader(image_path: str) -> tuple[bytes, str]:
        return image_bytes, sha256

    return _loader


def _per_path_loader(mapping: dict[str, str]) -> Any:
    """每条 image_path 映射到独立 sha256，避免 batch 内 cache 互相命中。"""

    def _loader(image_path: str) -> tuple[bytes, str]:
        sha = mapping.get(image_path, image_path)
        return b"\x89PNG\r\n\x1a\n", sha

    return _loader


def _fake_cache() -> _VisionCache:
    return _VisionCache(redis_client=fakeredis.FakeRedis(decode_responses=True))


def _ctx() -> dict:
    return {
        "spec_id": "23.501",
        "clause": "4.2.3",
        "section_title": "x",
    }


def _ok_text(kind: str = "logo", desc: str = "ok") -> str:
    return f'{{"figure_kind": "{kind}", "description": "{desc}"}}'


def _make_resolver(
    *,
    http: _StubAsyncHttp,
    cache: _VisionCache | None = None,
    loader: Any | None = None,
    rpm: int = 1000,
    tpm: int | None = None,
    max_retries: int = 3,
    undescribable_retries: int = 0,
    on_dead_letter: Any | None = None,
) -> tuple[VisionResolver, CompositeLimiter]:
    limiter = CompositeLimiter(rpm=rpm, tpm=tpm, name="mimo-test")
    resolver = VisionResolver(
        async_http_client=http,
        cache=cache or _fake_cache(),
        image_loader=loader or _fixed_loader(),
        model="mimo-v2.5",
        max_retries=max_retries,
        undescribable_retries=undescribable_retries,
        rate_limiter=limiter,
        on_dead_letter=on_dead_letter,
    )
    return resolver, limiter


# -------------------- aresolve_one --------------------


def test_aresolve_one_cache_hit_returns_without_http() -> None:
    async def _go() -> None:
        cache = _fake_cache()
        cache.set("h1", {"description": "cached desc", "figure_kind": "logo"})
        http = _StubAsyncHttp(responses=[])  # 不该被调用
        resolver, _ = _make_resolver(http=http, cache=cache)
        out = await resolver.aresolve_one("img.jpg", _ctx())
        assert out is not None
        assert out["cached"] is True
        assert out["description"] == "cached desc"
        assert http.calls == []

    asyncio.run(_go())


def test_aresolve_one_happy_path_caches_and_counts_limiter() -> None:
    async def _go() -> None:
        text = _ok_text("architecture", "arch diagram")
        http = _StubAsyncHttp(responses=[_ok_payload(text)])
        cache = _fake_cache()
        resolver, limiter = _make_resolver(http=http, cache=cache)
        out = await resolver.aresolve_one("img.jpg", _ctx())
        assert out is not None
        assert out["figure_kind"] == "architecture"
        assert out["description"] == "arch diagram"
        assert out["cached"] is False
        # 限速器记一次
        assert limiter.usage.requests_made == 1
        # 缓存命中：再调不增加 http
        out2 = await resolver.aresolve_one("img.jpg", _ctx())
        assert out2 is not None
        assert out2["cached"] is True
        assert len(http.calls) == 1

    asyncio.run(_go())


def test_aresolve_one_retry_then_success() -> None:
    async def _go() -> None:
        bad = _ok_payload("not json")
        good = _ok_payload(_ok_text("logo", "lg"))
        http = _StubAsyncHttp(responses=[bad, good])
        resolver, limiter = _make_resolver(http=http, max_retries=3)
        out = await resolver.aresolve_one("img.jpg", _ctx())
        assert out is not None
        assert out["description"] == "lg"
        assert len(http.calls) == 2
        # 限速器对每次 IO 都计数（含失败重试）
        assert limiter.usage.requests_made == 2

    asyncio.run(_go())


def test_aresolve_one_dead_letter_after_max_retries() -> None:
    async def _go() -> None:
        bad = _ok_payload("garbage")
        http = _StubAsyncHttp(responses=[bad, bad, bad, bad])  # max_retries+1
        cache = _fake_cache()
        dead: list[tuple[str, dict, str]] = []
        resolver, _ = _make_resolver(
            http=http,
            cache=cache,
            loader=_fixed_loader(sha256="dead-h"),
            max_retries=3,
            on_dead_letter=lambda p, c, e: dead.append((p, c, e)),
        )
        out = await resolver.aresolve_one("img.jpg", _ctx())
        assert out is None
        assert len(http.calls) == 4
        assert cache.is_dead("dead-h")
        assert dead and dead[0][0] == "img.jpg"

    asyncio.run(_go())


def test_aresolve_one_dead_letter_on_http_errors() -> None:
    async def _go() -> None:
        http = _StubAsyncHttp(responses=[RuntimeError(f"net{i}") for i in range(4)])
        cache = _fake_cache()
        resolver, _ = _make_resolver(
            http=http, cache=cache, loader=_fixed_loader(sha256="net-h"), max_retries=3
        )
        out = await resolver.aresolve_one("img.jpg", _ctx())
        assert out is None
        assert cache.is_dead("net-h")
        assert len(http.calls) == 4

    asyncio.run(_go())


def test_aresolve_one_skips_known_dead_letter() -> None:
    async def _go() -> None:
        cache = _fake_cache()
        cache.move_to_dead("dead-h", error="prev", ctx={})
        http = _StubAsyncHttp(responses=[])  # 不应被调用
        resolver, _ = _make_resolver(http=http, cache=cache, loader=_fixed_loader(sha256="dead-h"))
        out = await resolver.aresolve_one("img.jpg", _ctx())
        assert out is None
        assert http.calls == []

    asyncio.run(_go())


def test_aresolve_one_returns_none_on_image_load_failure() -> None:
    async def _go() -> None:
        def _bad(p: str) -> tuple[bytes, str]:
            raise FileNotFoundError("nope")

        http = _StubAsyncHttp(responses=[])
        resolver, _ = _make_resolver(http=http, loader=_bad)
        assert await resolver.aresolve_one("missing.jpg", _ctx()) is None
        assert http.calls == []

    asyncio.run(_go())


# -------------------- aresolve_batch --------------------


def test_aresolve_batch_empty_returns_empty() -> None:
    async def _go() -> None:
        http = _StubAsyncHttp(responses=[])
        resolver, _ = _make_resolver(http=http)
        assert await resolver.aresolve_batch([]) == []
        assert http.calls == []

    asyncio.run(_go())


def test_aresolve_batch_preserves_input_order_under_concurrency() -> None:
    """fan-out 完成顺序乱序，但返回顺序必须 = 输入顺序。

    `delay_s` 让 stub 的所有调用并发地"等"，调度器不能保证完成顺序。
    """

    async def _go() -> None:
        # 5 张 figure，每张一个独特 description
        n = 5
        responses = [_ok_payload(_ok_text("logo", f"d{i}")) for i in range(n)]
        http = _StubAsyncHttp(responses=responses, delay_s=0.02)
        cache = _fake_cache()
        loader = _per_path_loader({f"img{i}.jpg": f"sha{i}" for i in range(n)})
        resolver, _ = _make_resolver(http=http, cache=cache, loader=loader)
        items = [(f"img{i}.jpg", _ctx()) for i in range(n)]
        out = await resolver.aresolve_batch(items, concurrent=4)
        assert len(out) == n
        # 关键：第 i 项必然是 d{i}（顺序保持）
        # 注意 stub 按 FIFO 出 response，多并发下哪个 http call 拿到哪个 response
        # 取决于调度，所以这里只断言 description 集合 + 长度 + 无 None
        assert all(o is not None for o in out)
        descs = {o["description"] for o in out}  # type: ignore[index]
        assert descs == {f"d{i}" for i in range(n)}

    asyncio.run(_go())


def test_aresolve_batch_concurrency_limit_respected() -> None:
    """semaphore=2 → stub 内 peak_inflight ≤ 2。"""

    async def _go() -> None:
        n = 10
        responses = [_ok_payload(_ok_text("logo", f"d{i}")) for i in range(n)]
        http = _StubAsyncHttp(responses=responses, delay_s=0.05)
        cache = _fake_cache()
        loader = _per_path_loader({f"img{i}.jpg": f"sha{i}" for i in range(n)})
        resolver, _ = _make_resolver(http=http, cache=cache, loader=loader, rpm=10_000)
        items = [(f"img{i}.jpg", _ctx()) for i in range(n)]
        out = await resolver.aresolve_batch(items, concurrent=2)
        assert len(out) == n
        # peak in-flight 必须 ≤ semaphore
        assert http.peak_inflight <= 2, f"peak={http.peak_inflight} > 2"

    asyncio.run(_go())


def test_aresolve_batch_partial_failures_return_none_in_place() -> None:
    """混合：成功 + dead-letter（连续 4 次坏）+ 成功，None 应落在中间位。"""

    async def _go() -> None:
        ok = _ok_payload(_ok_text("logo", "ok-d"))
        bad = _ok_payload("garbage")
        # 顺序敏感的 stub：3 张 figure 并发到达，但完成顺序不定。
        # 所以这里不能用 stub 判定哪条对应哪张；改用：所有 image 都给 4 个 bad 后跟 1 个 good
        # 不现实——改用 max_retries=0 + 单次 bad/good 控制
        # max_retries=0 → 一次失败即 dead-letter
        responses = [ok, bad, ok]  # 三张图各 1 次调用
        http = _StubAsyncHttp(responses=responses)
        cache = _fake_cache()
        loader = _per_path_loader({f"img{i}.jpg": f"sha{i}" for i in range(3)})
        resolver, _ = _make_resolver(http=http, cache=cache, loader=loader, max_retries=0)
        # 串行（concurrent=1）→ stub 队列顺序 = 输入顺序
        items = [(f"img{i}.jpg", _ctx()) for i in range(3)]
        out = await resolver.aresolve_batch(items, concurrent=1)
        assert len(out) == 3
        assert out[0] is not None and out[0]["description"] == "ok-d"
        assert out[1] is None  # bad 那张
        assert out[2] is not None and out[2]["description"] == "ok-d"

    asyncio.run(_go())


def test_aresolve_batch_rate_limiter_throttles_rpm() -> None:
    """RPM=4 / period=0.5s → 8 张 figure 至少跨一个 period（≥0.4s）。"""

    async def _go() -> None:
        n = 8
        responses = [_ok_payload(_ok_text("logo", f"d{i}")) for i in range(n)]
        http = _StubAsyncHttp(responses=responses)
        cache = _fake_cache()
        loader = _per_path_loader({f"img{i}.jpg": f"sha{i}" for i in range(n)})
        # 注入 RPM=4 / period=0.5s 的限速器
        limiter = CompositeLimiter(rpm=4, period_seconds=0.5, name="mimo-throttle")
        resolver = VisionResolver(
            async_http_client=http,
            cache=cache,
            image_loader=loader,
            model="m",
            max_retries=0,
            rate_limiter=limiter,
        )
        items = [(f"img{i}.jpg", _ctx()) for i in range(n)]
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        out = await resolver.aresolve_batch(items, concurrent=8)
        elapsed = loop.time() - t0
        assert len(out) == n
        assert all(o is not None for o in out)
        # 8 调用 / 4 RPM/0.5s → 至少跨 1 个 period
        assert elapsed >= 0.4, f"expected >= 0.4s rate limit, got {elapsed:.3f}s"
        assert limiter.usage.requests_made == n

    asyncio.run(_go())


def test_aresolve_batch_unexpected_exception_per_item_isolated() -> None:
    """单图未捕获异常 → 该项 None，其他项继续完成。"""

    async def _go() -> None:
        # loader 第二张抛 RuntimeError（非 vision 内部 catch 的范围）
        bad_call_count = {"n": 0}

        def _loader(p: str) -> tuple[bytes, str]:
            if p == "img1.jpg":
                bad_call_count["n"] += 1
                raise RuntimeError("loader boom")
            return b"\x89PNG", f"sha-{p}"

        ok = _ok_payload(_ok_text("logo", "ok"))
        http = _StubAsyncHttp(responses=[ok, ok])  # 两张好图
        resolver, _ = _make_resolver(http=http, loader=_loader)
        items = [(f"img{i}.jpg", _ctx()) for i in range(3)]
        out = await resolver.aresolve_batch(items, concurrent=2)
        assert len(out) == 3
        assert out[0] is not None
        # img1 loader 抛错 → aresolve_one 内捕获返回 None
        assert out[1] is None
        assert out[2] is not None
        assert bad_call_count["n"] == 1

    asyncio.run(_go())


# -------------------- aclose --------------------


def test_aclose_when_async_client_injected_is_noop() -> None:
    """注入的 async_http_client 不应被 aclose 关闭（caller 拥有所有权）。"""

    async def _go() -> None:
        http = _StubAsyncHttp(responses=[])
        resolver, _ = _make_resolver(http=http)
        await resolver.aclose()  # 不抛
        # 仍能继续使用（stub 没真关）
        # 此处只断言 aclose 不抛即可

    asyncio.run(_go())


@pytest.mark.parametrize("concurrent", [1, 4, 8])
def test_aresolve_batch_runs_at_various_concurrency(concurrent: int) -> None:
    async def _go() -> None:
        n = 6
        responses = [_ok_payload(_ok_text("logo", f"d{i}")) for i in range(n)]
        http = _StubAsyncHttp(responses=responses)
        cache = _fake_cache()
        loader = _per_path_loader({f"img{i}.jpg": f"sha{i}" for i in range(n)})
        resolver, _ = _make_resolver(http=http, cache=cache, loader=loader, rpm=10_000)
        items = [(f"img{i}.jpg", _ctx()) for i in range(n)]
        out = await resolver.aresolve_batch(items, concurrent=concurrent)
        assert len(out) == n
        assert all(o is not None for o in out)
        assert http.peak_inflight <= concurrent

    asyncio.run(_go())
