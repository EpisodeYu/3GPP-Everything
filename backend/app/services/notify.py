"""Server酱³ 推送通道（用户每日对话配额通知用）。

与运维侧 `infra/monitor/notify.sh` 走同一条通道：POST 完整 `SERVERCHAN_URL`
（含 SendKey）的 `title` + `desp`（desp 支持 markdown）。

设计：
- `SERVERCHAN_URL` 为空 → no-op（功能降级，不报错）。
- 所有调用 **fire-and-forget + swallow**：Server酱 抖动 / 超时绝不阻塞或中断业务
  请求（与 `services/usage.py` 的计费 hook 同一容错哲学）。
- `send_serverchan` 暴露 `url` / `client` 形参供单测注入（生产路径走默认）。
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import get_settings

log = logging.getLogger(__name__)

_TIMEOUT_S = 8.0


async def send_serverchan(
    title: str,
    desp: str = "",
    *,
    url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """向 Server酱 推送一条消息；成功返回 True，未配置 / 失败返回 False（不抛）。

    `url` 缺省读 `Settings.SERVERCHAN_URL`；`client` 缺省自建（测试可注入带
    MockTransport 的 client）。
    """
    target = url if url is not None else get_settings().SERVERCHAN_URL.get_secret_value()
    if not target:
        return False

    owns_client = client is None
    cli = client or httpx.AsyncClient(timeout=_TIMEOUT_S)
    try:
        resp = await cli.post(target, data={"title": title[:255], "desp": desp})
        resp.raise_for_status()
        return True
    except Exception as exc:  # 推送失败绝不影响业务
        log.warning("serverchan push failed: %s", exc, exc_info=False)
        return False
    finally:
        if owns_client:
            await cli.aclose()


def schedule_serverchan(
    title: str, desp: str = "", *, url: str | None = None
) -> asyncio.Task[bool] | None:
    """fire-and-forget 推送：不阻塞调用方。

    `url` 缺省读 `Settings.SERVERCHAN_URL`；调用方可显式传入请求级 settings 的值，
    使其遵循 dependency override / 测试隔离。
    未配置（空）→ 返回 None，不创建 task（避免测试里留悬挂 task / 误发真实推送）。
    无运行中的 event loop → 返回 None（释放 coro 防 unawaited warning）。
    """
    resolved = url if url is not None else get_settings().SERVERCHAN_URL.get_secret_value()
    if not resolved:
        return None
    coro = send_serverchan(title, desp, url=resolved)
    try:
        return asyncio.ensure_future(coro)
    except RuntimeError:
        if hasattr(coro, "close"):
            coro.close()
        return None


__all__ = ["schedule_serverchan", "send_serverchan"]
