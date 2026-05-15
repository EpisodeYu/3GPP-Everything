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

import base64
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ingestion.hf_loader.image_resolver import hash_bytes, resolve_image

from .prompts import PROMPT_E_UNIFIED, normalize_vision_payload, parse_vision_json

log = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "tgpp:vision"
CACHE_KEY_OK = f"{CACHE_KEY_PREFIX}:{{sha256}}"
CACHE_KEY_RETRY = f"{CACHE_KEY_PREFIX}:retry:{{sha256}}"
CACHE_KEY_DEAD = f"{CACHE_KEY_PREFIX}:dead:{{sha256}}"

DEFAULT_MAX_TOKENS = 8192
DEFAULT_HTTP_TIMEOUT_S = 180.0
DEFAULT_MAX_RETRIES = 3


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


def call_mimo_unified(
    client: _LiteLLMClient,
    *,
    image_bytes: bytes,
    model: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    image_mime: str = "image/jpeg",
) -> tuple[dict, dict]:
    """调一次 mimo（PROMPT_E_UNIFIED）。返回 (raw_payload, usage_meta)。"""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
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
    payload = client.chat(body)
    choice = payload["choices"][0]
    msg = choice["message"]
    text = (msg.get("content") or "").strip()
    finish_reason = choice.get("finish_reason")
    usage = payload.get("usage") or {}
    meta = {
        "model": payload.get("model", model),
        "completion_tokens": usage.get("completion_tokens", 0),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        ),
        "finish_reason": finish_reason,
        "raw_text": text,
    }
    return payload, meta


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
        cache: _VisionCache | None = None,
        image_loader: ImageBytesLoader | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        image_mime: str = "image/jpeg",
        on_dead_letter: Callable[[str, dict, str], None] | None = None,
    ) -> None:
        self._http = http_client or self._build_default_http_client()
        self._owns_http = http_client is None
        self._cache = cache if cache is not None else _VisionCache()
        self._image_loader = image_loader or make_default_image_loader(
            token=os.environ.get("HF_TOKEN") or None,
        )
        self._model = model or os.environ.get("LLM_VISION_MODEL") or "mimo-v2.5"
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._image_mime = image_mime
        self._on_dead_letter = on_dead_letter

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

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

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
        last_error: str = ""
        # max_retries 表示"重试次数"，所以总尝试次数 = max_retries + 1
        for attempt in range(self._max_retries + 1):
            try:
                payload, meta = call_mimo_unified(
                    self._http,
                    image_bytes=image_bytes,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    image_mime=self._image_mime,
                )
            except Exception as exc:
                last_error = f"http_error: {type(exc).__name__}: {exc}"
                log.warning(
                    "vision: HTTP fail (attempt %d/%d) for %s: %s",
                    attempt + 1,
                    self._max_retries + 1,
                    image_path,
                    last_error,
                )
            else:
                if meta["finish_reason"] == "length":
                    last_error = (
                        f"finish_reason=length completion_tokens={meta['completion_tokens']} "
                        f"reasoning_tokens={meta['reasoning_tokens']}"
                    )
                    log.warning(
                        "vision: truncated (attempt %d/%d) for %s: %s",
                        attempt + 1,
                        self._max_retries + 1,
                        image_path,
                        last_error,
                    )
                else:
                    parsed = parse_vision_json(meta["raw_text"])
                    norm = normalize_vision_payload(parsed) if parsed is not None else None
                    if norm is None:
                        snippet = (meta["raw_text"] or "")[:200]
                        last_error = f"json_parse_or_schema_fail: {snippet!r}"
                        log.warning(
                            "vision: parse fail (attempt %d/%d) for %s: %s",
                            attempt + 1,
                            self._max_retries + 1,
                            image_path,
                            last_error,
                        )
                    else:
                        return VisionResult(
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
            # 失败：累计 retry 计数
            self._cache.bump_retry(sha256, error=last_error, ctx=_prune_ctx(ctx))

        # 全部尝试用尽 → dead-letter
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
    )


# -------------------- 序列化辅助（CLI / debug 用） --------------------


def vision_result_to_json(result: VisionResult, *, include_raw: bool = False) -> dict:
    d = asdict(result)
    if not include_raw:
        d.pop("raw_response", None)
    return d
