"""公司日 token 预算。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow


class BudgetStore(Protocol):
    async def get_daily_limit(self, company_id: uuid.UUID) -> int | None:
        """None = 未配置（拒绝调用，需先设预算）。"""
        ...

    async def get_usage(self, company_id: uuid.UUID, day: dt.date) -> int: ...

    async def set_daily_limit(self, company_id: uuid.UUID, daily_tokens: int) -> None: ...

    async def reserve_and_add(self, company_id: uuid.UUID, day: dt.date, tokens: int) -> None:
        """检查未超限后累加；超限抛 RATE_LIMITED。"""
        ...


class MemoryBudgetStore:
    def __init__(self) -> None:
        self.limits: dict[uuid.UUID, int] = {}
        self.usage: dict[tuple[uuid.UUID, dt.date], int] = {}

    async def get_daily_limit(self, company_id: uuid.UUID) -> int | None:
        return self.limits.get(company_id)

    async def get_usage(self, company_id: uuid.UUID, day: dt.date) -> int:
        return self.usage.get((company_id, day), 0)

    async def set_daily_limit(self, company_id: uuid.UUID, daily_tokens: int) -> None:
        if daily_tokens < 0:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "daily_tokens 须 ≥ 0")
        self.limits[company_id] = daily_tokens

    async def reserve_and_add(self, company_id: uuid.UUID, day: dt.date, tokens: int) -> None:
        if tokens < 0:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "tokens 须 ≥ 0")
        limit = self.limits.get(company_id)
        if limit is None:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "公司未配置 LLM token 日预算")
        used = self.usage.get((company_id, day), 0)
        if used + tokens > limit:
            raise PlatformError(ErrorCode.RATE_LIMITED, "超过公司日 token 预算")
        self.usage[(company_id, day)] = used + tokens


class PgBudgetStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def get_daily_limit(self, company_id: uuid.UUID) -> int | None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            row = (
                await conn.execute(
                    text("SELECT daily_tokens FROM llm.company_token_budget WHERE company_id = :c"),
                    {"c": company_id},
                )
            ).first()
            return int(row[0]) if row else None

    async def get_usage(self, company_id: uuid.UUID, day: dt.date) -> int:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            row = (
                await conn.execute(
                    text(
                        "SELECT tokens_used FROM llm.company_token_usage "
                        "WHERE company_id = :c AND usage_date = :d"
                    ),
                    {"c": company_id, "d": day},
                )
            ).first()
            return int(row[0]) if row else 0

    async def set_daily_limit(self, company_id: uuid.UUID, daily_tokens: int) -> None:
        if daily_tokens < 0:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "daily_tokens 须 ≥ 0")
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            await conn.execute(
                text(
                    "INSERT INTO llm.company_token_budget (company_id, daily_tokens, updated_at) "
                    "VALUES (:c, :n, :t) ON CONFLICT (company_id) DO UPDATE "
                    "SET daily_tokens = :n, updated_at = :t"
                ),
                {"c": company_id, "n": daily_tokens, "t": utcnow()},
            )

    async def reserve_and_add(self, company_id: uuid.UUID, day: dt.date, tokens: int) -> None:
        if tokens < 0:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "tokens 须 ≥ 0")
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            lim = (
                await conn.execute(
                    text("SELECT daily_tokens FROM llm.company_token_budget WHERE company_id = :c FOR UPDATE"),
                    {"c": company_id},
                )
            ).first()
            if lim is None:
                raise PlatformError(ErrorCode.PRECONDITION_FAILED, "公司未配置 LLM token 日预算")
            limit = int(lim[0])
            row = (
                await conn.execute(
                    text(
                        "SELECT tokens_used FROM llm.company_token_usage "
                        "WHERE company_id = :c AND usage_date = :d FOR UPDATE"
                    ),
                    {"c": company_id, "d": day},
                )
            ).first()
            used = int(row[0]) if row else 0
            if used + tokens > limit:
                raise PlatformError(ErrorCode.RATE_LIMITED, "超过公司日 token 预算")
            await conn.execute(
                text(
                    "INSERT INTO llm.company_token_usage (company_id, usage_date, tokens_used, updated_at) "
                    "VALUES (:c, :d, :n, :t) "
                    "ON CONFLICT (company_id, usage_date) DO UPDATE "
                    "SET tokens_used = llm.company_token_usage.tokens_used + :n, updated_at = :t"
                ),
                {"c": company_id, "d": day, "n": tokens, "t": utcnow()},
            )


async def set_budget_on_conn(conn: AsyncConnection, company_id: uuid.UUID, daily_tokens: int) -> None:
    if daily_tokens < 0:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "daily_tokens 须 ≥ 0")
    await conn.execute(
        text(
            "INSERT INTO llm.company_token_budget (company_id, daily_tokens, updated_at) "
            "VALUES (:c, :n, :t) ON CONFLICT (company_id) DO UPDATE "
            "SET daily_tokens = :n, updated_at = :t"
        ),
        {"c": company_id, "n": daily_tokens, "t": utcnow()},
    )


async def get_budget_on_conn(conn: AsyncConnection, company_id: uuid.UUID) -> int | None:
    row = (
        await conn.execute(
            text("SELECT daily_tokens FROM llm.company_token_budget WHERE company_id = :c"), {"c": company_id}
        )
    ).first()
    return int(row[0]) if row else None


async def get_usage_on_conn(conn: AsyncConnection, company_id: uuid.UUID, day: dt.date) -> int:
    row = (
        await conn.execute(
            text(
                "SELECT tokens_used FROM llm.company_token_usage WHERE company_id = :c AND usage_date = :d"
            ),
            {"c": company_id, "d": day},
        )
    ).first()
    return int(row[0]) if row else 0
