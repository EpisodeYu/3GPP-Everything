"""ApiUsage 写入链路（M7.4）。

文档锚点：`docs/03-development/06-evaluation-and-observability.md §9.1`。

设计：
- 4 路 hook：`record_llm_usage` / `record_embedding_usage` / `record_rerank_usage` /
  `record_web_search_usage`，由 `LiteLLMClient` / `web_search_tool` 在响应处调用。
- 用户身份：`current_user_id` `ContextVar` 在 chat 路由入口（`api/v1/chat.py`）
  设置；ContextVar 在 asyncio Task 内自动传递（FastAPI 单 request 单 task），
  hook 读到后才写入。**没有 user 上下文（如 ingestion / eval / 后台 job）→ skip**。
- 写入：按 `(user_id, day=今天 UTC)` upsert ApiUsage。先尝试 UPDATE；rowcount=0
  退化为 INSERT；并发 INSERT 撞 UNIQUE 约束 → rollback 后再 UPDATE 一次。
  跨 PG / SQLite 兼容，不依赖 `ON CONFLICT` 方言语法。
- 失败容忍：任何异常都 log + swallow，**绝不让计费 hook 阻断业务调用**（CLAUDE.md
  §3 surgical changes 原则；M4.10 §9.2 alerts 仅 log warning，配套 Q2 决策）。

Voyage rerank token 口径：`query_tokens × n_docs + Σ doc_tokens`（见 §9.1）。
ApiUsage schema 没单独的 `rerank_tokens`，所以 rerank 调用累计到 `rerank_calls += 1`
+ `total_cost_usd += billable_tokens × per_token`，token 数本身用 structlog
观测；如未来要拆，加 `rerank_tokens` 列再迁移即可。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import get_sessionmaker
from app.db.models import ApiUsage
from app.llm.pricing import (
    embedding_cost_usd,
    llm_cost_usd,
    rerank_billable_tokens,
    rerank_cost_usd,
    web_search_cost_usd,
)

log = logging.getLogger(__name__)

# Context-bound 当前 user id；FastAPI 请求 task 内部 set，hook 内 get。
# 在 chat 路由处一次设值，ContextVar 自动随 asyncio Task 切换传递。
current_user_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "tgpp_current_user_id", default=None
)


def set_current_user(user_id: uuid.UUID | None) -> contextvars.Token[uuid.UUID | None]:
    """在请求入口设置当前 user_id；返回 token 供 finally 中 reset。"""
    return current_user_id.set(user_id)


def reset_current_user(token: contextvars.Token[uuid.UUID | None]) -> None:
    current_user_id.reset(token)


def get_current_user_id() -> uuid.UUID | None:
    return current_user_id.get()


# 测试钩子：让 unit 测试能注入 sessionmaker，不污染全局。生产路径下保持 None。
_sessionmaker_override: async_sessionmaker[AsyncSession] | None = None


def set_sessionmaker_override(sm: async_sessionmaker[AsyncSession] | None) -> None:
    """测试 / lifespan 注入；None 退回 `get_sessionmaker()`（lru_cache 单例）。"""
    global _sessionmaker_override
    _sessionmaker_override = sm


def _resolve_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return _sessionmaker_override or get_sessionmaker()


def _today_utc() -> date:
    return datetime.now(UTC).date()


async def _upsert_api_usage(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    day: date,
    llm_input: int = 0,
    llm_output: int = 0,
    embedding_tokens: int = 0,
    rerank_calls: int = 0,
    web_search_calls: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """按 `(user_id, day)` upsert ApiUsage（PG / SQLite 通用路径）。

    走两阶段：先 UPDATE（atomic 累加），rowcount=0 退化 INSERT；INSERT 撞 UNIQUE
    约束（极端并发 race）→ rollback 后再 UPDATE 一次。整体在调用方传入的 session
    上 commit，调用方负责异常隔离。
    """
    res = await db.execute(
        update(ApiUsage)
        .where(ApiUsage.user_id == user_id, ApiUsage.day == day)
        .values(
            llm_input_tokens=ApiUsage.llm_input_tokens + llm_input,
            llm_output_tokens=ApiUsage.llm_output_tokens + llm_output,
            embedding_tokens=ApiUsage.embedding_tokens + embedding_tokens,
            rerank_calls=ApiUsage.rerank_calls + rerank_calls,
            web_search_calls=ApiUsage.web_search_calls + web_search_calls,
            total_cost_usd=ApiUsage.total_cost_usd + cost_usd,
        )
    )
    rowcount = getattr(res, "rowcount", 0) or 0
    if rowcount > 0:
        await db.commit()
        return

    new_row = ApiUsage(
        user_id=user_id,
        day=day,
        llm_input_tokens=llm_input,
        llm_output_tokens=llm_output,
        embedding_tokens=embedding_tokens,
        rerank_calls=rerank_calls,
        web_search_calls=web_search_calls,
        total_cost_usd=cost_usd,
    )
    db.add(new_row)
    try:
        await db.commit()
        return
    except IntegrityError:
        await db.rollback()
        # 并发竞态：另一个 task 先把 (user_id, day) 行写进去了；再做一次 UPDATE 累加
        await db.execute(
            update(ApiUsage)
            .where(ApiUsage.user_id == user_id, ApiUsage.day == day)
            .values(
                llm_input_tokens=ApiUsage.llm_input_tokens + llm_input,
                llm_output_tokens=ApiUsage.llm_output_tokens + llm_output,
                embedding_tokens=ApiUsage.embedding_tokens + embedding_tokens,
                rerank_calls=ApiUsage.rerank_calls + rerank_calls,
                web_search_calls=ApiUsage.web_search_calls + web_search_calls,
                total_cost_usd=ApiUsage.total_cost_usd + cost_usd,
            )
        )
        await db.commit()


async def _do_record(
    *,
    user_id: uuid.UUID,
    llm_input: int = 0,
    llm_output: int = 0,
    embedding_tokens: int = 0,
    rerank_calls: int = 0,
    web_search_calls: int = 0,
    cost_usd: float = 0.0,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    sm = sessionmaker or _resolve_sessionmaker()
    try:
        async with sm() as db:
            await _upsert_api_usage(
                db,
                user_id=user_id,
                day=_today_utc(),
                llm_input=llm_input,
                llm_output=llm_output,
                embedding_tokens=embedding_tokens,
                rerank_calls=rerank_calls,
                web_search_calls=web_search_calls,
                cost_usd=cost_usd,
            )
    except Exception as exc:
        log.warning("usage._do_record failed: %s", exc, exc_info=False)


# === public hooks ===


async def record_llm_usage(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    user_id: uuid.UUID | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """LLM chat completion → 累加 input/output tokens + cost。

    `user_id` 缺省读 `current_user_id` ContextVar；仍为 None → skip（agent 在
    ingestion / eval 等无 user 场景下也会调 LiteLLM，不写库）。
    """
    uid = user_id or get_current_user_id()
    if uid is None:
        return
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return
    cost = llm_cost_usd(model, prompt_tokens, completion_tokens)
    await _do_record(
        user_id=uid,
        llm_input=max(prompt_tokens, 0),
        llm_output=max(completion_tokens, 0),
        cost_usd=cost,
        sessionmaker=sessionmaker,
    )


async def record_embedding_usage(
    *,
    model: str,
    tokens: int,
    user_id: uuid.UUID | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Embedding → 累加 embedding_tokens + cost。"""
    uid = user_id or get_current_user_id()
    if uid is None or tokens <= 0:
        return
    cost = embedding_cost_usd(model, tokens)
    await _do_record(
        user_id=uid,
        embedding_tokens=max(tokens, 0),
        cost_usd=cost,
        sessionmaker=sessionmaker,
    )


async def record_rerank_usage(
    *,
    model: str,
    query_tokens: int,
    doc_tokens: int,
    n_docs: int,
    user_id: uuid.UUID | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Rerank → `rerank_calls += 1`，按 Voyage 口径计 billable tokens 折算 cost。

    ApiUsage schema 不存 rerank token 数（M4.10 设计），仅 calls 自增 + cost 累加；
    token 数走 structlog 观测。
    """
    uid = user_id or get_current_user_id()
    if uid is None:
        return
    billable = rerank_billable_tokens(
        query_tokens=query_tokens, doc_tokens=doc_tokens, n_docs=n_docs
    )
    cost = rerank_cost_usd(model, query_tokens=query_tokens, doc_tokens=doc_tokens, n_docs=n_docs)
    log.debug(
        "usage.rerank model=%s n_docs=%d q_tokens=%d d_tokens=%d billable=%d cost=%.6f",
        model,
        n_docs,
        query_tokens,
        doc_tokens,
        billable,
        cost,
    )
    await _do_record(
        user_id=uid,
        rerank_calls=1,
        cost_usd=cost,
        sessionmaker=sessionmaker,
    )


async def record_web_search_usage(
    *,
    provider: str = "tavily-search",
    calls: int = 1,
    user_id: uuid.UUID | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """WebSearch → `web_search_calls += calls`，按 provider per-call 单价计 cost。"""
    uid = user_id or get_current_user_id()
    if uid is None or calls <= 0:
        return
    cost = web_search_cost_usd(provider, calls=calls)
    await _do_record(
        user_id=uid,
        web_search_calls=max(calls, 0),
        cost_usd=cost,
        sessionmaker=sessionmaker,
    )


# === fire-and-forget helper ===


def schedule_usage_hook(coro: Any) -> asyncio.Task[None] | None:
    """fire-and-forget 调用入口；调用方拿到 Task 后通常忽略，仅测试 await。

    无运行 event loop 时（如同步上下文里误调）→ 返回 None，不抛错。
    """
    try:
        return asyncio.ensure_future(coro)
    except RuntimeError:
        # event loop 未启动；释放 coro 防 unawaited warning
        if hasattr(coro, "close"):
            coro.close()
        return None


# === aggregation helpers（alerts daily job 使用）===


async def aggregate_today_cost_usd(db: AsyncSession, *, day: date | None = None) -> float:
    from sqlalchemy import func

    target = day or _today_utc()
    res = await db.execute(
        select(func.coalesce(func.sum(ApiUsage.total_cost_usd), 0.0)).where(ApiUsage.day == target)
    )
    return float(res.scalar_one() or 0.0)


async def aggregate_month_cost_usd(db: AsyncSession, *, today: date | None = None) -> float:
    from sqlalchemy import func

    today = today or _today_utc()
    month_start = today.replace(day=1)
    res = await db.execute(
        select(func.coalesce(func.sum(ApiUsage.total_cost_usd), 0.0)).where(
            ApiUsage.day >= month_start, ApiUsage.day <= today
        )
    )
    return float(res.scalar_one() or 0.0)


__all__ = [
    "aggregate_month_cost_usd",
    "aggregate_today_cost_usd",
    "current_user_id",
    "get_current_user_id",
    "record_embedding_usage",
    "record_llm_usage",
    "record_rerank_usage",
    "record_web_search_usage",
    "reset_current_user",
    "schedule_usage_hook",
    "set_current_user",
    "set_sessionmaker_override",
]
