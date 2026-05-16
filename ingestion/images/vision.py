"""Vision pipeline（方案 E）。

实现 docs/03-development/02-ingestion-and-indexing.md §4.2 的方案 E：

- mimo-v2.5 单次调用同时输出 description + 结构化字段
- **不注入 GSMA caption**（v2 benchmark 实测会传染 GSMA 错误，见 §4.2.1 注释）
- Redis 缓存 `tgpp:vision:{sha256}` 永久 TTL
- 失败队列 `tgpp:vision:retry:{sha256}`（含 retry_count + last_error）；
  重试 3 次后落 `tgpp:vision:dead:{sha256}`
- 接口契约符合 chunker `figure.py::vision_resolver`：`__call__(image_path, ctx) -> dict | None`

`figure_kind == 'undescribable'` 不是失败：照常缓存（避免反复调），但
`raw_extra["vision"]["figure_kind"]` 让 chunker 决定是否退化（默认仍写 description）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ingestion.hf_loader.image_resolver import hash_bytes, resolve_image
from ingestion.rate_limit import CompositeLimiter, get_mimo_limiter

from .prompts import PROMPT_E_UNIFIED, normalize_vision_payload, parse_vision_json

log = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "tgpp:vision"
CACHE_KEY_OK = f"{CACHE_KEY_PREFIX}:{{sha256}}"
CACHE_KEY_RETRY = f"{CACHE_KEY_PREFIX}:retry:{{sha256}}"
CACHE_KEY_DEAD = f"{CACHE_KEY_PREFIX}:dead:{{sha256}}"

DEFAULT_MAX_TOKENS = 8192
DEFAULT_HTTP_TIMEOUT_S = 180.0
DEFAULT_MAX_RETRIES = 3
# mimo-v2.5 偶发把可见图片误判为 undescribable（见 38.331 POC handoff §6.3）。
# 命中 undescribable 时额外再调 N 次；N 次都还是 undescribable 才接受并缓存。
DEFAULT_UNDESCRIBABLE_RETRIES = 3


# -------------------- 数据契约（与 §4.2.2 对齐） --------------------


@dataclass(slots=True)
class VisionResult:
    """单次 vision 调用结果。"""

    description: str
    figure_kind: str
    visible_labels: list[str] = field(default_factory=list)
    visible_acronyms: list[str] = field(default_factory=list)
    spec_role: str = ""
    undescribable_reason: str = ""
    model: str = ""
    completion_tokens: int = 0
    reasoning_tokens: int | None = None
    cached: bool = False
    raw_response: dict | None = None

    def to_chunker_dict(self) -> dict:
        """对齐 chunker `figure.build_figure_content` 期待的字段（精简，不含 raw_response）。"""
        return {
            "description": self.description,
            "figure_kind": self.figure_kind,
            "visible_labels": list(self.visible_labels),
            "visible_acronyms": list(self.visible_acronyms),
            "spec_role": self.spec_role,
            "undescribable_reason": self.undescribable_reason,
            "model": self.model,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached": self.cached,
        }


class VisionError(Exception):
    """vision pipeline 抛出的对外异常。chunker `figure.py` 已 try/except 兜底。"""


class VisionDeadLetterError(VisionError):
    """重试 3 次仍失败，已写 dead-letter；调用方应跳过本图。"""


# -------------------- HTTP 客户端 --------------------


class _LiteLLMClient:
    """薄封装 httpx.Client，调用 LiteLLM proxy 的 chat/completions。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> _LiteLLMClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def chat(self, body: dict) -> dict:
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


class _AsyncLiteLLMClient:
    """异步薄封装；与 _LiteLLMClient 平行。

    单例 client 跨多个 fan-out 调用复用（HTTP/2 + keepalive）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat(self, body: dict) -> dict:
        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


# -------------------- 图片字节读取（resolver 注入点） --------------------


ImageBytesLoader = Callable[[str], tuple[bytes, str]]
"""签名：(image_path) -> (bytes, sha256)。

默认实现走 `hf_loader.resolve_image` 拉 HF 缓存；测试可注入 mock。
"""


def make_default_image_loader(
    *,
    revision: str | None = None,
    token: str | None = None,
    cache_dir: str | Path | None = None,
) -> ImageBytesLoader:
    """默认 loader：HF 路径 → resolve_image 拉本地 → 读 bytes + 算 sha256。"""

    def _loader(image_path: str) -> tuple[bytes, str]:
        local = Path(image_path)
        if local.is_file():
            data = local.read_bytes()
            return data, hash_bytes(data)
        img = resolve_image(image_path, revision=revision, token=token, cache_dir=cache_dir)
        return img.local_path.read_bytes(), img.sha256

    return _loader


# -------------------- Redis 缓存层 --------------------


class _VisionCache:
    """Redis 缓存 + retry/dead-letter 队列。

    - cache hit：直接返回 dict
    - cache miss：返回 None
    - get_retry_count / bump_retry / move_to_dead：失败重试管理

    redis_client 留作可选注入（测试用 fakeredis）；不传则按 REDIS_URL 自连。
    若 REDIS_URL 未配置 → cache disabled，所有读写 no-op（开发环境兜底）。
    """

    def __init__(self, redis_client: Any | None = None, *, redis_url: str | None = None) -> None:
        self._client = redis_client
        if self._client is None:
            url = redis_url or os.environ.get("REDIS_URL")
            if url:
                try:
                    import redis as _redis
                except Exception:  # pragma: no cover - 依赖已锁定，理论上不会触发
                    log.warning("redis package missing; vision cache disabled")
                    return
                try:
                    self._client = _redis.Redis.from_url(url, decode_responses=True)
                except Exception as exc:  # pragma: no cover - 配置错误兜底
                    log.warning("redis connect failed (%s); vision cache disabled", exc)
                    self._client = None
            else:
                log.warning("REDIS_URL not set; vision cache disabled")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def get(self, sha256: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            raw = self._client.get(CACHE_KEY_OK.format(sha256=sha256))
        except Exception as exc:  # pragma: no cover
            log.warning("redis get failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("vision cache for %s corrupted; will re-call", sha256[:8])
            return None

    def set(self, sha256: str, payload: dict) -> None:
        if not self.enabled:
            return
        try:
            self._client.set(CACHE_KEY_OK.format(sha256=sha256), json.dumps(payload))
            self._client.delete(CACHE_KEY_RETRY.format(sha256=sha256))
        except Exception as exc:  # pragma: no cover
            log.warning("redis set failed: %s", exc)

    def get_retry_count(self, sha256: str) -> int:
        if not self.enabled:
            return 0
        try:
            raw = self._client.get(CACHE_KEY_RETRY.format(sha256=sha256))
        except Exception:  # pragma: no cover
            return 0
        if not raw:
            return 0
        try:
            return int(json.loads(raw).get("retry_count", 0))
        except Exception:
            return 0

    def bump_retry(self, sha256: str, *, error: str, ctx: dict) -> int:
        """记录一次失败，返回累计重试次数（已自增后的值）。"""
        count = self.get_retry_count(sha256) + 1
        if not self.enabled:
            return count
        payload = {
            "retry_count": count,
            "last_error": error,
            "last_at": int(time.time()),
            "ctx": ctx,
        }
        try:
            self._client.set(CACHE_KEY_RETRY.format(sha256=sha256), json.dumps(payload))
        except Exception as exc:  # pragma: no cover
            log.warning("redis bump_retry failed: %s", exc)
        return count

    def move_to_dead(self, sha256: str, *, error: str, ctx: dict) -> None:
        if not self.enabled:
            return
        payload = {
            "retry_count": self.get_retry_count(sha256),
            "last_error": error,
            "moved_at": int(time.time()),
            "ctx": ctx,
        }
        try:
            self._client.set(CACHE_KEY_DEAD.format(sha256=sha256), json.dumps(payload))
            self._client.delete(CACHE_KEY_RETRY.format(sha256=sha256))
        except Exception as exc:  # pragma: no cover
            log.warning("redis move_to_dead failed: %s", exc)

    def is_dead(self, sha256: str) -> bool:
        if not self.enabled:
            return False
        try:
            return bool(self._client.exists(CACHE_KEY_DEAD.format(sha256=sha256)))
        except Exception:  # pragma: no cover
            return False


# -------------------- 单次 mimo 调用 --------------------


def _build_mimo_body(image_bytes: bytes, *, model: str, max_tokens: int, image_mime: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT_E_UNIFIED},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image_mime};base64,{b64}"},
                    },
                ],
            }
        ],
    }


def _extract_meta(payload: dict, model: str) -> dict:
    choice = payload["choices"][0]
    msg = choice["message"]
    text = (msg.get("content") or "").strip()
    finish_reason = choice.get("finish_reason")
    usage = payload.get("usage") or {}
    return {
        "model": payload.get("model", model),
        "completion_tokens": usage.get("completion_tokens", 0),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "finish_reason": finish_reason,
        "raw_text": text,
    }


def call_mimo_unified(
    client: _LiteLLMClient,
    *,
    image_bytes: bytes,
    model: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    image_mime: str = "image/jpeg",
) -> tuple[dict, dict]:
    """调一次 mimo（PROMPT_E_UNIFIED）。返回 (raw_payload, usage_meta)。"""
    body = _build_mimo_body(image_bytes, model=model, max_tokens=max_tokens, image_mime=image_mime)
    payload = client.chat(body)
    return payload, _extract_meta(payload, model)


async def acall_mimo_unified(
    client: _AsyncLiteLLMClient,
    *,
    image_bytes: bytes,
    model: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    image_mime: str = "image/jpeg",
) -> tuple[dict, dict]:
    """async 版 call_mimo_unified。返回 (raw_payload, usage_meta)。"""
    body = _build_mimo_body(image_bytes, model=model, max_tokens=max_tokens, image_mime=image_mime)
    payload = await client.chat(body)
    return payload, _extract_meta(payload, model)


# -------------------- VisionResolver（chunker 接入点） --------------------


class VisionResolver:
    """符合 chunker `figure.py::vision_resolver` 接口签名的 callable。

    构造参数：
      - http_client: 注入的 _LiteLLMClient；若为 None 则按 .env 自建
      - cache: 注入的 _VisionCache；若为 None 则按 .env REDIS_URL 自建
      - image_loader: 注入的 ImageBytesLoader；若为 None 则用 hf_loader 默认 loader
      - model: vision 模型；缺省读 LLM_VISION_MODEL，再缺省 'mimo-v2.5'
      - max_tokens: 单次调用 max_tokens（避免 reasoning 截断；不增加成本，按实际计费）
      - max_retries: 失败重试上限（含首次共调用 max_retries+1 次；超过 → dead-letter）
      - undescribable_retries: 收到 figure_kind=undescribable 时额外重试次数。
        mimo-v2.5 偶发把可见图片误判为 undescribable（见 POC handoff §6.3），
        全部重试仍 undescribable 才接受最终结果（不进 dead-letter，照常缓存）
      - image_mime: figure 文件扩展名 → MIME 映射；默认 jpeg
      - on_dead_letter: 进 dead-letter 时的回调（监控用）

    调用：
      resolver = VisionResolver(...)
      result_dict = resolver(image_path, ctx)   # 返回 dict 或 None

    返回：
      - dict（含 §4.2.2 全字段 + cached 标记 + model / completion_tokens 等 audit 信息）
      - None：不可恢复（dead-letter 已写 / 输入图片读失败）
    """

    def __init__(
        self,
        *,
        http_client: _LiteLLMClient | None = None,
        async_http_client: _AsyncLiteLLMClient | None = None,
        cache: _VisionCache | None = None,
        image_loader: ImageBytesLoader | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        undescribable_retries: int = DEFAULT_UNDESCRIBABLE_RETRIES,
        image_mime: str = "image/jpeg",
        on_dead_letter: Callable[[str, dict, str], None] | None = None,
        rate_limiter: CompositeLimiter | None = None,
    ) -> None:
        # sync / async client 都 lazy 构造：只走 async 入口的 resolver 不需要 sync env
        self._http: _LiteLLMClient | None = http_client
        self._owns_http = False
        self._async_http = async_http_client
        self._owns_async_http = False
        self._cache = cache if cache is not None else _VisionCache()
        self._image_loader = image_loader or make_default_image_loader(
            token=os.environ.get("HF_TOKEN") or None,
        )
        self._model = model or os.environ.get("LLM_VISION_MODEL") or "mimo-v2.5"
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._undescribable_retries = max(0, int(undescribable_retries))
        self._image_mime = image_mime
        self._on_dead_letter = on_dead_letter
        # async fan-out 用的 mimo 限速器；测试可注入；prod 走 get_mimo_limiter() 单例
        self._rate_limiter = rate_limiter

    @staticmethod
    def _build_default_http_client() -> _LiteLLMClient:
        base_url = os.environ.get("LITELLM_BASE_URL")
        api_key = os.environ.get("LITELLM_API_KEY")
        if not base_url or not api_key:
            raise VisionError(
                "LITELLM_BASE_URL / LITELLM_API_KEY missing; "
                "either configure .env or pass http_client= explicitly"
            )
        return _LiteLLMClient(base_url=base_url, api_key=api_key)

    @staticmethod
    def _build_default_async_http_client() -> _AsyncLiteLLMClient:
        base_url = os.environ.get("LITELLM_BASE_URL")
        api_key = os.environ.get("LITELLM_API_KEY")
        if not base_url or not api_key:
            raise VisionError(
                "LITELLM_BASE_URL / LITELLM_API_KEY missing; "
                "either configure .env or pass async_http_client= explicitly"
            )
        return _AsyncLiteLLMClient(base_url=base_url, api_key=api_key)

    def _ensure_sync_client(self) -> _LiteLLMClient:
        if self._http is None:
            self._http = self._build_default_http_client()
            self._owns_http = True
        return self._http

    def _ensure_async_client(self) -> _AsyncLiteLLMClient:
        if self._async_http is None:
            self._async_http = self._build_default_async_http_client()
            self._owns_async_http = True
        return self._async_http

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            self._http.close()

    async def aclose(self) -> None:
        """关闭可能持有的 async client。生产 pipeline 在 worker 退出时调一次即可。"""
        self.close()
        if self._owns_async_http and self._async_http is not None:
            await self._async_http.aclose()
            self._async_http = None
            self._owns_async_http = False

    def __enter__(self) -> VisionResolver:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __call__(self, image_path: str, ctx: dict) -> dict | None:
        try:
            image_bytes, sha256 = self._image_loader(image_path)
        except Exception as exc:
            log.warning("vision: image load failed for %s: %s", image_path, exc)
            return None

        if self._cache.is_dead(sha256):
            log.info("vision: skipping dead-letter image %s (%s)", image_path, sha256[:8])
            return None

        cached = self._cache.get(sha256)
        if cached is not None:
            cached = dict(cached)
            cached["cached"] = True
            return cached

        try:
            result = self._call_with_retry(
                image_path=image_path, image_bytes=image_bytes, sha256=sha256, ctx=ctx
            )
        except VisionDeadLetterError:
            return None
        out = result.to_chunker_dict()
        # 缓存（不含 raw_response，节省 Redis 体积）
        self._cache.set(sha256, out)
        return out

    def _call_with_retry(
        self, *, image_path: str, image_bytes: bytes, sha256: str, ctx: dict
    ) -> VisionResult:
        """两类"重试"独立计数：
        - hard_failures（HTTP error / length 截断 / JSON 解析失败）：
          达到 max_retries+1 → dead-letter
        - undescribable_attempts（mimo 误判 figure_kind=undescribable）：
          额外多调 N 次，全部仍 undescribable 才接受最终结果，**不进** dead-letter
        """
        last_error: str = ""
        hard_failures = 0
        undescribable_attempts = 0
        last_undescribable: VisionResult | None = None
        # 总尝试次数硬上限，防退化为死循环
        max_total_attempts = (self._max_retries + 1) + self._undescribable_retries
        attempt_idx = 0
        client = self._ensure_sync_client()

        while attempt_idx < max_total_attempts:
            attempt_idx += 1
            try:
                payload, meta = call_mimo_unified(
                    client,
                    image_bytes=image_bytes,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    image_mime=self._image_mime,
                )
            except Exception as exc:
                last_error = f"http_error: {type(exc).__name__}: {exc}"
                hard_failures += 1
                log.warning(
                    "vision: HTTP fail (hard %d/%d) for %s: %s",
                    hard_failures,
                    self._max_retries + 1,
                    image_path,
                    last_error,
                )
                self._cache.bump_retry(sha256, error=last_error, ctx=_prune_ctx(ctx))
                if hard_failures > self._max_retries:
                    break
                continue

            if meta["finish_reason"] == "length":
                last_error = (
                    f"finish_reason=length completion_tokens={meta['completion_tokens']} "
                    f"reasoning_tokens={meta['reasoning_tokens']}"
                )
                hard_failures += 1
                log.warning(
                    "vision: truncated (hard %d/%d) for %s: %s",
                    hard_failures,
                    self._max_retries + 1,
                    image_path,
                    last_error,
                )
                self._cache.bump_retry(sha256, error=last_error, ctx=_prune_ctx(ctx))
                if hard_failures > self._max_retries:
                    break
                continue

            parsed = parse_vision_json(meta["raw_text"])
            norm = normalize_vision_payload(parsed) if parsed is not None else None
            if norm is None:
                snippet = (meta["raw_text"] or "")[:200]
                last_error = f"json_parse_or_schema_fail: {snippet!r}"
                hard_failures += 1
                log.warning(
                    "vision: parse fail (hard %d/%d) for %s: %s",
                    hard_failures,
                    self._max_retries + 1,
                    image_path,
                    last_error,
                )
                self._cache.bump_retry(sha256, error=last_error, ctx=_prune_ctx(ctx))
                if hard_failures > self._max_retries:
                    break
                continue

            result = VisionResult(
                description=norm["description"],
                figure_kind=norm["figure_kind"],
                visible_labels=norm["visible_labels"],
                visible_acronyms=norm["visible_acronyms"],
                spec_role=norm["spec_role"],
                undescribable_reason=norm["undescribable_reason"],
                model=meta["model"],
                completion_tokens=meta["completion_tokens"],
                reasoning_tokens=meta["reasoning_tokens"],
                cached=False,
                raw_response=payload,
            )

            if (
                norm["figure_kind"] == "undescribable"
                and undescribable_attempts < self._undescribable_retries
            ):
                undescribable_attempts += 1
                last_undescribable = result
                log.warning(
                    "vision: undescribable result (retry %d/%d) for %s; reason=%r",
                    undescribable_attempts,
                    self._undescribable_retries,
                    image_path,
                    norm["undescribable_reason"],
                )
                continue

            return result

        # 所有 hard 失败用尽：若期间已拿到过 undescribable 结果，**优先返回它**
        # 而不是丢进 dead-letter（图像本身可用，下游 chunker 仍能用 description）
        if last_undescribable is not None:
            log.warning(
                "vision: hard failures exhausted after seeing undescribable for %s; "
                "accepting last undescribable result",
                image_path,
            )
            return last_undescribable

        # 全部尝试用尽且没有任何成功结果 → dead-letter
        log.error(
            "vision: dead-letter image=%s sha256=%s error=%s", image_path, sha256[:8], last_error
        )
        self._cache.move_to_dead(sha256, error=last_error, ctx=_prune_ctx(ctx))
        if self._on_dead_letter is not None:
            try:
                self._on_dead_letter(image_path, ctx, last_error)
            except Exception:  # pragma: no cover - 监控回调不应炸主流程
                log.exception("on_dead_letter callback failed")
        raise VisionDeadLetterError(last_error)

    # -------------------- async 入口（M2 §4.8） --------------------

    async def aresolve_one(self, image_path: str, ctx: dict) -> dict | None:
        """async 版 `__call__`：含 cache hit / dead-letter 跳过 / mimo 限速。

        `pipeline_concurrent` 层调，`figure.py` chunker 同步入口不变。
        """
        try:
            image_bytes, sha256 = self._image_loader(image_path)
        except Exception as exc:
            log.warning("vision(async): image load failed for %s: %s", image_path, exc)
            return None

        if self._cache.is_dead(sha256):
            log.info("vision(async): skip dead-letter %s (%s)", image_path, sha256[:8])
            return None

        cached = self._cache.get(sha256)
        if cached is not None:
            cached = dict(cached)
            cached["cached"] = True
            return cached

        try:
            result = await self._acall_with_retry(
                image_path=image_path, image_bytes=image_bytes, sha256=sha256, ctx=ctx
            )
        except VisionDeadLetterError:
            return None
        out = result.to_chunker_dict()
        self._cache.set(sha256, out)
        return out

    async def aresolve_batch(
        self,
        items: Sequence[tuple[str, dict]],
        *,
        concurrent: int = 8,
    ) -> list[dict | None]:
        """fan-out N 张 figure：semaphore 限并发 + mimo 全局限速。

        返回顺序与 `items` 一致；失败 / dead-letter 项返回 None（与 sync `__call__` 保持一致）。
        """
        if not items:
            return []
        sem = asyncio.Semaphore(max(1, concurrent))

        async def _one(idx: int, image_path: str, ctx: dict) -> tuple[int, dict | None]:
            async with sem:
                try:
                    out = await self.aresolve_one(image_path, ctx)
                except Exception as exc:  # 兜底：单图未捕获异常不应炸 batch
                    log.exception("vision(async) unexpected error for %s: %s", image_path, exc)
                    out = None
                return idx, out

        tasks = [_one(i, p, c) for i, (p, c) in enumerate(items)]
        gathered = await asyncio.gather(*tasks)
        # 已按 idx 收集；按 idx 排序回原顺序
        gathered.sort(key=lambda x: x[0])
        return [out for _, out in gathered]

    async def _acall_with_retry(
        self, *, image_path: str, image_bytes: bytes, sha256: str, ctx: dict
    ) -> VisionResult:
        """async 版 _call_with_retry；与 sync 同语义，仅 IO 不同。"""
        last_error: str = ""
        hard_failures = 0
        undescribable_attempts = 0
        last_undescribable: VisionResult | None = None
        max_total_attempts = (self._max_retries + 1) + self._undescribable_retries
        attempt_idx = 0
        client = self._ensure_async_client()
        limiter = self._rate_limiter or get_mimo_limiter()

        while attempt_idx < max_total_attempts:
            attempt_idx += 1
            try:
                async with limiter.with_rate_limit():
                    payload, meta = await acall_mimo_unified(
                        client,
                        image_bytes=image_bytes,
                        model=self._model,
                        max_tokens=self._max_tokens,
                        image_mime=self._image_mime,
                    )
            except Exception as exc:
                last_error = f"http_error: {type(exc).__name__}: {exc}"
                hard_failures += 1
                log.warning(
                    "vision(async): HTTP fail (hard %d/%d) for %s: %s",
                    hard_failures,
                    self._max_retries + 1,
                    image_path,
                    last_error,
                )
                self._cache.bump_retry(sha256, error=last_error, ctx=_prune_ctx(ctx))
                if hard_failures > self._max_retries:
                    break
                continue

            if meta["finish_reason"] == "length":
                last_error = (
                    f"finish_reason=length completion_tokens={meta['completion_tokens']} "
                    f"reasoning_tokens={meta['reasoning_tokens']}"
                )
                hard_failures += 1
                self._cache.bump_retry(sha256, error=last_error, ctx=_prune_ctx(ctx))
                if hard_failures > self._max_retries:
                    break
                continue

            parsed = parse_vision_json(meta["raw_text"])
            norm = normalize_vision_payload(parsed) if parsed is not None else None
            if norm is None:
                snippet = (meta["raw_text"] or "")[:200]
                last_error = f"json_parse_or_schema_fail: {snippet!r}"
                hard_failures += 1
                self._cache.bump_retry(sha256, error=last_error, ctx=_prune_ctx(ctx))
                if hard_failures > self._max_retries:
                    break
                continue

            result = VisionResult(
                description=norm["description"],
                figure_kind=norm["figure_kind"],
                visible_labels=norm["visible_labels"],
                visible_acronyms=norm["visible_acronyms"],
                spec_role=norm["spec_role"],
                undescribable_reason=norm["undescribable_reason"],
                model=meta["model"],
                completion_tokens=meta["completion_tokens"],
                reasoning_tokens=meta["reasoning_tokens"],
                cached=False,
                raw_response=payload,
            )
            if (
                norm["figure_kind"] == "undescribable"
                and undescribable_attempts < self._undescribable_retries
            ):
                undescribable_attempts += 1
                last_undescribable = result
                continue
            return result

        if last_undescribable is not None:
            return last_undescribable

        log.error(
            "vision(async): dead-letter image=%s sha256=%s error=%s",
            image_path,
            sha256[:8],
            last_error,
        )
        self._cache.move_to_dead(sha256, error=last_error, ctx=_prune_ctx(ctx))
        if self._on_dead_letter is not None:
            try:
                self._on_dead_letter(image_path, ctx, last_error)
            except Exception:  # pragma: no cover
                log.exception("on_dead_letter callback failed")
        raise VisionDeadLetterError(last_error)


def _prune_ctx(ctx: dict) -> dict:
    """ctx 进 Redis 前删除非必要字段（surrounding_paragraph 等可能很长）。"""
    keep = {"spec_id", "clause", "section_title", "image_alt", "spec_caption"}
    return {k: v for k, v in ctx.items() if k in keep and v}


# -------------------- 便捷工厂 --------------------


def build_resolver_from_env(
    *,
    revision: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    undescribable_retries: int = DEFAULT_UNDESCRIBABLE_RETRIES,
) -> VisionResolver:
    """读 .env 一次性构造 VisionResolver（生产 / CLI 用）。

    revision 透传给 hf_loader 默认 image_loader；其他参数走环境变量。
    """
    image_loader = make_default_image_loader(
        revision=revision,
        token=os.environ.get("HF_TOKEN") or None,
    )
    return VisionResolver(
        image_loader=image_loader,
        max_tokens=max_tokens,
        max_retries=max_retries,
        undescribable_retries=undescribable_retries,
    )


# -------------------- 序列化辅助（CLI / debug 用） --------------------


def vision_result_to_json(result: VisionResult, *, include_raw: bool = False) -> dict:
    d = asdict(result)
    if not include_raw:
        d.pop("raw_response", None)
    return d
