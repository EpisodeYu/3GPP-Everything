"""`app.services.usage` 单测：4 路 hook + ContextVar + ApiUsage upsert 行为（M7.4）。

文档锚点：`docs/03-development/06-evaluation-and-observability.md §9.1`。

覆盖：
- ContextVar set/get/reset 基本行为
- 4 路 record_*_usage 写入 ApiUsage（ContextVar 注入 user）
- 同 (user_id, day) 第二次调用走 UPDATE 累加，不撞 UNIQUE
- 跨 day 两条独立行
- 缺 user 上下文 → skip（不抛错）
- 0 token / 0 calls 的入参 → skip（避免空写）
- LLM cost 计算覆盖 mimo-v2.5-pro
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import ApiUsage, Base, User
from app.services import usage as usage_mod


@pytest_asyncio.fixture
async def sm() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture
async def user_id(sm: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    uid = uuid.uuid4()
    async with sm() as db:
        db.add(
            User(
                id=uid,
                username="alice",
                password_hash="x",
                role="user",
            )
        )
        await db.commit()
    return uid


async def _read_today_row(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID, day: date | None = None
) -> ApiUsage | None:
    target = day or datetime.now(UTC).date()
    async with sm() as db:
        res = await db.execute(
            select(ApiUsage).where(ApiUsage.user_id == user_id, ApiUsage.day == target)
        )
        return res.scalar_one_or_none()


# === ContextVar 基本行为 ===


class TestContextVar:
    def test_default_is_none(self) -> None:
        assert usage_mod.get_current_user_id() is None

    def test_set_and_reset(self) -> None:
        uid = uuid.uuid4()
        token = usage_mod.set_current_user(uid)
        try:
            assert usage_mod.get_current_user_id() == uid
        finally:
            usage_mod.reset_current_user(token)
        assert usage_mod.get_current_user_id() is None


# === 4 路 hook 写入 ===


class TestRecordLLMUsage:
    async def test_writes_row_when_user_in_context(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        token = usage_mod.set_current_user(user_id)
        try:
            await usage_mod.record_llm_usage(
                model="mimo-v2.5-pro",
                prompt_tokens=1000,
                completion_tokens=500,
                sessionmaker=sm,
            )
        finally:
            usage_mod.reset_current_user(token)

        row = await _read_today_row(sm, user_id)
        assert row is not None
        assert row.llm_input_tokens == 1000
        assert row.llm_output_tokens == 500
        # mimo-v2.5-pro: 1.0/M × 1000 + 3.0/M × 500 = 1e-3 + 1.5e-3 = 2.5e-3
        assert pytest.approx(row.total_cost_usd, rel=1e-6) == 0.0025

    async def test_explicit_user_id_overrides_contextvar(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_llm_usage(
            model="mimo-v2.5-pro",
            prompt_tokens=10,
            completion_tokens=5,
            user_id=user_id,
            sessionmaker=sm,
        )
        row = await _read_today_row(sm, user_id)
        assert row is not None
        assert row.llm_input_tokens == 10

    async def test_no_user_context_skips(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        # ContextVar 未 set → skip
        await usage_mod.record_llm_usage(
            model="mimo-v2.5-pro",
            prompt_tokens=1000,
            completion_tokens=500,
            sessionmaker=sm,
        )
        row = await _read_today_row(sm, user_id)
        assert row is None

    async def test_zero_tokens_skips(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_llm_usage(
            model="mimo-v2.5-pro",
            prompt_tokens=0,
            completion_tokens=0,
            user_id=user_id,
            sessionmaker=sm,
        )
        row = await _read_today_row(sm, user_id)
        assert row is None

    async def test_unknown_model_records_zero_cost(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_llm_usage(
            model="dream-9999",
            prompt_tokens=1000,
            completion_tokens=500,
            user_id=user_id,
            sessionmaker=sm,
        )
        row = await _read_today_row(sm, user_id)
        assert row is not None
        assert row.llm_input_tokens == 1000
        assert row.total_cost_usd == 0.0


class TestRecordEmbeddingUsage:
    async def test_voyage_free_tier_records_tokens_zero_cost(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_embedding_usage(
            model="voyage-4-large",
            tokens=10_000,
            user_id=user_id,
            sessionmaker=sm,
        )
        row = await _read_today_row(sm, user_id)
        assert row is not None
        assert row.embedding_tokens == 10_000
        assert row.total_cost_usd == 0.0

    async def test_glm_embedding_billed(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_embedding_usage(
            model="embedding-3",
            tokens=1_000_000,
            user_id=user_id,
            sessionmaker=sm,
        )
        row = await _read_today_row(sm, user_id)
        assert row is not None
        # 0.5/M × 1M = 0.5
        assert pytest.approx(row.total_cost_usd) == 0.5

    async def test_zero_tokens_skips(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_embedding_usage(
            model="voyage-4-large",
            tokens=0,
            user_id=user_id,
            sessionmaker=sm,
        )
        assert await _read_today_row(sm, user_id) is None


class TestRecordRerankUsage:
    async def test_increments_calls_and_uses_voyage_formula(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_rerank_usage(
            model="rerank-2.5",
            query_tokens=100,
            doc_tokens=4000,
            n_docs=5,
            user_id=user_id,
            sessionmaker=sm,
        )
        row = await _read_today_row(sm, user_id)
        assert row is not None
        assert row.rerank_calls == 1
        # voyage rerank-2.5 免费区 billed=False → cost=0
        assert row.total_cost_usd == 0.0

    async def test_two_calls_accumulate(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        for _ in range(3):
            await usage_mod.record_rerank_usage(
                model="rerank-2.5",
                query_tokens=20,
                doc_tokens=200,
                n_docs=10,
                user_id=user_id,
                sessionmaker=sm,
            )
        row = await _read_today_row(sm, user_id)
        assert row is not None
        assert row.rerank_calls == 3


class TestRecordWebSearchUsage:
    async def test_tavily_per_call(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_web_search_usage(
            provider="tavily-search",
            calls=2,
            user_id=user_id,
            sessionmaker=sm,
        )
        row = await _read_today_row(sm, user_id)
        assert row is not None
        assert row.web_search_calls == 2
        # $0.01 × 2
        assert pytest.approx(row.total_cost_usd) == 0.02

    async def test_zero_calls_skips(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_web_search_usage(
            provider="tavily-search",
            calls=0,
            user_id=user_id,
            sessionmaker=sm,
        )
        assert await _read_today_row(sm, user_id) is None


# === upsert 累加行为 ===


class TestUpsertAccumulation:
    async def test_same_day_second_call_updates_existing(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_llm_usage(
            model="mimo-v2.5",
            prompt_tokens=100,
            completion_tokens=50,
            user_id=user_id,
            sessionmaker=sm,
        )
        await usage_mod.record_llm_usage(
            model="mimo-v2.5",
            prompt_tokens=200,
            completion_tokens=70,
            user_id=user_id,
            sessionmaker=sm,
        )

        async with sm() as db:
            res = await db.execute(select(ApiUsage).where(ApiUsage.user_id == user_id))
            rows = res.scalars().all()
        assert len(rows) == 1, "expected single row per (user_id, day)"
        assert rows[0].llm_input_tokens == 300
        assert rows[0].llm_output_tokens == 120

    async def test_mixed_kinds_share_one_row(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        await usage_mod.record_llm_usage(
            model="mimo-v2.5",
            prompt_tokens=10,
            completion_tokens=5,
            user_id=user_id,
            sessionmaker=sm,
        )
        await usage_mod.record_embedding_usage(
            model="voyage-4-large", tokens=200, user_id=user_id, sessionmaker=sm
        )
        await usage_mod.record_rerank_usage(
            model="rerank-2.5",
            query_tokens=10,
            doc_tokens=100,
            n_docs=3,
            user_id=user_id,
            sessionmaker=sm,
        )
        await usage_mod.record_web_search_usage(
            provider="tavily-search", calls=1, user_id=user_id, sessionmaker=sm
        )

        async with sm() as db:
            res = await db.execute(select(ApiUsage))
            rows = res.scalars().all()
        assert len(rows) == 1
        r = rows[0]
        assert r.llm_input_tokens == 10
        assert r.llm_output_tokens == 5
        assert r.embedding_tokens == 200
        assert r.rerank_calls == 1
        assert r.web_search_calls == 1


# === aggregation helpers（alerts daily job 用）===


class TestAggregation:
    async def test_today_and_month_sum_match_seeded_rows(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        today = datetime.now(UTC).date()
        async with sm() as db:
            db.add(ApiUsage(user_id=user_id, day=today, total_cost_usd=1.5))
            db.add(
                ApiUsage(
                    user_id=user_id,
                    day=today.replace(day=1) if today.day != 1 else today,
                    total_cost_usd=2.0,
                )
            )
            await db.commit()

        async with sm() as db:
            today_cost = await usage_mod.aggregate_today_cost_usd(db, day=today)
            month_cost = await usage_mod.aggregate_month_cost_usd(db, today=today)
        assert pytest.approx(today_cost) == (3.5 if today.day == 1 else 1.5)
        assert pytest.approx(month_cost) == 3.5


# === LiteLLMClient hook 路径 smoke ===


class TestLiteLLMClientUsageHook:
    async def test_chat_response_triggers_record_llm_usage(
        self, sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
    ) -> None:
        """LiteLLMClient.chat() 在响应里看到 usage 字段 → fire-and-forget hook 写入。

        全程 mock httpx；usage_mod.set_sessionmaker_override 让 hook 用 in-memory DB。
        """
        import json

        import httpx

        from app.core.config import Settings
        from app.llm.litellm_client import LiteLLMClient

        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 45},
                },
            )

        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            LITELLM_BASE_URL="http://test/v1",
            LITELLM_API_KEY="sk",
        )
        usage_mod.set_sessionmaker_override(sm)
        token = usage_mod.set_current_user(user_id)
        try:
            async with LiteLLMClient(
                settings=settings,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ) as cli:
                resp = await cli.chat(
                    messages=[{"role": "user", "content": "hi"}], model="mimo-v2.5"
                )
                # 等 fire-and-forget Task 完成
                import asyncio

                pending = [
                    t
                    for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and not t.done()
                ]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        finally:
            usage_mod.reset_current_user(token)
            usage_mod.set_sessionmaker_override(None)

        assert resp["choices"][0]["message"]["content"] == "ok"
        row = await _read_today_row(sm, user_id)
        assert row is not None, "usage hook should have written ApiUsage"
        assert row.llm_input_tokens == 123
        assert row.llm_output_tokens == 45
