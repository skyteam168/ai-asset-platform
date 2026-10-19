"""控制面数据库访问：每个事务都带 RLS 上下文（architecture.md 2.6）。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from aip.libs.common.config import get_settings


@dataclass(frozen=True)
class DbScope:
    """事务可见的公司集合。bypass 只用于系统任务（快照构建、启动引导）与总部全局角色。"""

    company_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    bypass: bool = False

    @staticmethod
    def system() -> DbScope:
        return DbScope(bypass=True)


class Database:
    def __init__(self, url: str | None = None) -> None:
        self.engine: AsyncEngine = create_async_engine(url or get_settings().app_database_url, pool_pre_ping=True)

    @asynccontextmanager
    async def tx(self, scope: DbScope) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.company_ids', :ids, true), set_config('app.bypass_rls', :b, true)"),
                {"ids": ",".join(str(c) for c in sorted(scope.company_ids)), "b": "on" if scope.bypass else "off"},
            )
            yield conn

    async def dispose(self) -> None:
        await self.engine.dispose()
