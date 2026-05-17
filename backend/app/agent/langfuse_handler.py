"""Langfuse 集成（M4.5）。

口径 = `docs/03-development/03-agent.md §8`。Langfuse v4 与文档原 v2 例子 API 不同：
- v4 用全局 `Langfuse()` 单例 + `langfuse.langchain.CallbackHandler`（不再在 handler
  里传 public_key / session_id），session_id / user_id 通过 RunnableConfig 的
  `metadata`：`{"langfuse_session_id": ..., "langfuse_user_id": ...}` 注入
- 缺 key → 返回 None；调用方在 graph.astream_events 时若 handler=None 不传 callbacks

接口：
    init_langfuse(settings) -> Langfuse | None
        进程级懒初始化全局 client；缺任一 key 返回 None；幂等
    build_callback_handler(settings) -> CallbackHandler | None
        构造 langchain CallbackHandler；client 未配置时返回 None
    build_trace_metadata(*, session_id=None, user_id=None, mode=None, extra=None)
        生成 RunnableConfig 用的 metadata dict（key 命名遵守 langfuse v4 约定）
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

_client_lock = Lock()
_client: Any | None = None
_client_init_failed = False


def init_langfuse(settings: Settings | None = None) -> Any | None:
    """进程级懒初始化 Langfuse 全局 client；缺 key / 失败 → None。"""
    global _client, _client_init_failed
    if _client is not None:
        return _client
    if _client_init_failed:
        return None

    with _client_lock:
        if _client is not None:
            return _client
        if _client_init_failed:
            return None
        s = settings or get_settings()
        pk = s.LANGFUSE_PUBLIC_KEY.get_secret_value().strip()
        sk = s.LANGFUSE_SECRET_KEY.get_secret_value().strip()
        host = s.LANGFUSE_HOST.strip()
        if not pk or not sk:
            log.info("langfuse keys missing, tracing disabled")
            _client_init_failed = True
            return None
        try:
            from langfuse import Langfuse

            _client = Langfuse(public_key=pk, secret_key=sk, host=host)
            return _client
        except Exception as exc:
            log.warning("langfuse client init failed: %s", exc)
            _client_init_failed = True
            return None


def build_callback_handler(settings: Settings | None = None) -> Any | None:
    """构造 langchain CallbackHandler；client 未配置 / import 失败 → None。"""
    client = init_langfuse(settings)
    if client is None:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as exc:
        log.warning("langfuse CallbackHandler unavailable: %s", exc)
        return None


def build_trace_metadata(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    mode: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 RunnableConfig.metadata；langfuse v4 识别 `langfuse_*` 前缀字段。"""
    meta: dict[str, Any] = {"app": "tgpp"}
    if session_id is not None:
        meta["langfuse_session_id"] = session_id
    if user_id is not None:
        meta["langfuse_user_id"] = user_id
    if mode is not None:
        meta["mode"] = mode
    if extra:
        meta.update(extra)
    return meta


def _reset_for_tests() -> None:
    """单测专用：清掉单例状态。"""
    global _client, _client_init_failed
    with _client_lock:
        _client = None
        _client_init_failed = False
