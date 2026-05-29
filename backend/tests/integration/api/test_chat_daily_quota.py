"""集成测：普通用户每日对话配额 100→429 + admin 豁免 + Server酱边界通知。

走完整 chat 依赖链（`rate_limit("chat")` + `daily_chat_quota()`）。用 bogus session
让请求穿过两个限流依赖后在 handler 落 404，从而**无需 agent 图**即可压测配额边界
（未越界 → 404，越界 → 429 在 handler 之前）。

`DAILY_CHAT_LIMIT` 经 `get_settings` override 压到很小值；`schedule_serverchan`
monkeypatch 成记录器，隔离真实 Server酱网络并断言通知在边界触发。
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings

from .test_auth import _bootstrap_admin, _login
from .test_chat import _auth_headers, _new_user_token


def _set_daily_limit(app: Any, limit: int) -> None:
    """把当前 test settings 复制一份、仅把 DAILY_CHAT_LIMIT 压到 `limit`。"""
    base = app.dependency_overrides[get_settings]()
    low = base.model_copy(update={"DAILY_CHAT_LIMIT": limit})
    app.dependency_overrides[get_settings] = lambda: low


async def test_daily_chat_quota_blocks_over_limit_and_notifies(
    app_and_state: Any, monkeypatch: Any
) -> None:
    app, _, _ = app_and_state
    limit = 3
    _set_daily_limit(app, limit)

    pushes: list[str] = []
    monkeypatch.setattr(
        "app.services.notify.schedule_serverchan",
        lambda title, desp="", url=None: pushes.append(title),  # type: ignore[func-returns-value]
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _new_user_token(client, username="quota_user")
        bogus = str(uuid.uuid4())

        # 前 limit 次：穿过配额依赖（未越界）→ 命中不存在会话 → 404
        for i in range(limit):
            r = await client.post(
                f"/api/v1/sessions/{bogus}/messages",
                json={"content": f"q{i}"},
                headers=_auth_headers(token),
            )
            assert r.status_code == 404, (i, r.status_code, r.text)

        # 第 limit+1 次：被每日配额拦下 → 429（在 handler 之前）
        r = await client.post(
            f"/api/v1/sessions/{bogus}/messages",
            json={"content": "over"},
            headers=_auth_headers(token),
        )
        assert r.status_code == 429, r.text
        body = r.json()
        assert body["code"] == "rate_limited"

        # 再来一次仍 429（已越界，但不再重复推送）
        r2 = await client.post(
            f"/api/v1/sessions/{bogus}/messages",
            json={"content": "over2"},
            headers=_auth_headers(token),
        )
        assert r2.status_code == 429

    # Server酱：当日首次 + 首次越界各推一次，共 2 条（第 5 次越界不重复推）
    assert len(pushes) == 2, pushes
    assert "今日首次对话" in pushes[0]
    assert "超出每日对话上限" in pushes[1]


async def test_admin_is_exempt_from_daily_quota(app_and_state: Any, monkeypatch: Any) -> None:
    app, fake_redis, _ = app_and_state
    _set_daily_limit(app, 2)

    pushes: list[str] = []
    monkeypatch.setattr(
        "app.services.notify.schedule_serverchan",
        lambda title, desp="", url=None: pushes.append(title),  # type: ignore[func-returns-value]
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _bootstrap_admin(client)
        admin = await _login(client, "admin1", "passw0rd!")
        token = str(admin["access_token"])
        bogus = str(uuid.uuid4())

        # 远超 limit=2：admin 不受限，全部 404，绝不 429
        for i in range(5):
            r = await client.post(
                f"/api/v1/sessions/{bogus}/messages",
                json={"content": f"a{i}"},
                headers=_auth_headers(token),
            )
            assert r.status_code == 404, (i, r.status_code)

    # admin 既不计入 chat_daily，也不触发任何通知
    assert pushes == []
    assert not any("chat_daily" in k for k in fake_redis.store)
