"""配额（architecture.md 4.1、8.1 执行链中的“限流与配额”）。

按 UTC 日计数，键：consumer:{id}（消费方每日调用）、consumer_write:{id}（每日写操作）、ceiling:{id}（同一授权上限
下全公司共享的每日调用）。一次调用涉及的所有计数在同一事务里条件自增：任何一项已到上限就整体不计数并拒绝，
因此被拒的调用不消耗额度；多副本共享计数（PostgreSQL），开发模式可用进程内实现。
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

QuotaItem = tuple[str, str, int]  # (key, company_id, limit)


class QuotaStore(Protocol):
    async def take(self, day: dt.date, items: list[QuotaItem]) -> str | None:
        """全部未超限则各加一并返回 None；否则不改任何计数，返回第一个已满的键。"""
        ...


class MemoryQuotaStore:
    def __init__(self) -> None:
        self.counts: dict[tuple[dt.date, str], int] = defaultdict(int)

    async def take(self, day: dt.date, items: list[QuotaItem]) -> str | None:
        for key, _, limit in items:
            if self.counts[(day, key)] >= limit:
                return key
        for key, _, _ in items:
            self.counts[(day, key)] += 1
        return None


class _Exceeded(Exception):
    def __init__(self, key: str) -> None:
        self.key = key


class PgQuotaStore:
    SQL = text(
        """INSERT INTO exec.usage_counter (day, key, company_id, count) VALUES (:day, :key, :company, 1)
           ON CONFLICT (day, key) DO UPDATE SET count = exec.usage_counter.count + 1, updated_at = now()
           WHERE exec.usage_counter.count < :limit
           RETURNING count"""
    )

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def take(self, day: dt.date, items: list[QuotaItem]) -> str | None:
        if not items:
            return None
        try:
            async with self.engine.begin() as conn:
                await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
                for key, company, limit in items:
                    row = (
                        await conn.execute(self.SQL, {"day": day, "key": key, "company": company, "limit": limit})
                    ).first()
                    if row is None:
                        raise _Exceeded(key)  # 回滚本事务里已加的计数
        except _Exceeded as e:
            return e.key
        return None
