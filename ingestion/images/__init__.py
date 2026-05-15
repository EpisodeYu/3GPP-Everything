"""Vision pipeline (mimo-v2.5 + GSMA images)。

公共入口：

- `VisionResolver` 符合 chunker `figure.py::vision_resolver` 接口签名，
  内部走 LiteLLM proxy + Redis 缓存 + 重试 + dead-letter。
- `VisionResult` 单次调用结果数据类。
- `PROMPT_E_UNIFIED` 当前生产 prompt（与 docs §4.2.1 对齐）。
- `build_resolver_from_env` 读 .env 直接构造（CLI / 全量索引用）。

详见 docs/03-development/02-ingestion-and-indexing.md §4.2。
"""

from .prompts import PROMPT_E_UNIFIED, normalize_vision_payload, parse_vision_json
from .vision import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TOKENS,
    VisionDeadLetterError,
    VisionError,
    VisionResolver,
    VisionResult,
    build_resolver_from_env,
    call_mimo_unified,
    make_default_image_loader,
    vision_result_to_json,
)

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_TOKENS",
    "PROMPT_E_UNIFIED",
    "VisionDeadLetterError",
    "VisionError",
    "VisionResolver",
    "VisionResult",
    "build_resolver_from_env",
    "call_mimo_unified",
    "make_default_image_loader",
    "normalize_vision_payload",
    "parse_vision_json",
    "vision_result_to_json",
]
