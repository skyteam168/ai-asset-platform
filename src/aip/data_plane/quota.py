"""配额与调用计量（architecture.md 4.1、7.4、8.1）。

按 UTC 日计数，键：
- consumer:{id} / consumer_write:{id} / ceiling:{id}：有上限的配额（take 条件自增）
- asset:{asset_id}:version:{version_id}：资产版本调用量（record 无上限，供退役门禁）

配额：一次调用涉及的所有计数在同一事务里条件自增；任何一项已到上限就整体不计数并拒绝。
计量：成功调用后 record，不拒绝调用。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

QuotaItem = tuple[str, str, int]  # (key, company_id, limit)


def asset_version_meter_key(asset_id: uuid.UUID | str, version_id: uuid.UUID | str) -> str:
    return f"asset:{asset_id}:version:{version_id}"


class QuotaStore(Protocol):
    async def take(self, day: dt.date, items: list[QuotaItem]) -> str | None:
        """全部未超限则各加一并返回 None；否则不改任何计数，返回第一个已满的键。"""
        ...

    async def record(self, day: dt.date, key: str, company_id: str, n: int = 1) -> None:
        """无上限累加（资产版本调用计量）。"""
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

    async def record(self, day: dt.date, key: str, company_id: str, n: int = 1) -> None:
        del company_id  # 内存实现不隔离公司
        self.counts[(day, key)] += n


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
    RECORD = text(
        """INSERT INTO exec.usage_counter (day, key, company_id, count) VALUES (:day, :key, :company, :n)
           ON CONFLICT (day, key) DO UPDATE
           SET count = exec.usage_counter.count + EXCLUDED.count, updated_at = now()"""
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

    async def record(self, day: dt.date, key: str, company_id: str, n: int = 1) -> None:
        if n <= 0:
            return
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            await conn.execute(self.RECORD, {"day": day, "key": key, "company": company_id, "n": n})


async def sum_meter_since(conn: AsyncConnection, key: str, *, since: dt.date) -> int:
    """控制面查询资产版本近窗调用量（需已 SET 公司/bypass）。"""
    row = (
        await conn.execute(
            text("SELECT COALESCE(SUM(count), 0) FROM exec.usage_counter WHERE key = :k AND day >= :since"),
            {"k": key, "since": since},
        )
    ).first()
    return int(row[0]) if row else 0
