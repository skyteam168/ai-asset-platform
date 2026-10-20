"""快照长轮询（architecture.md 4.4、ADR-0019）：数据面带 wait_s 拉取，控制面在 LISTEN aip_snapshot 上等待变更。

每个控制面进程只保持一条 LISTEN 连接，唤醒所有等待中的请求；没有变更时到时返回 204。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.db import Database, DbScope

log = logging.getLogger("aip.snapshot_feed")
CHANNEL = "aip_snapshot"
MAX_WAIT_S = 25.0


class SnapshotFeed:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._cond = asyncio.Condition()
        self._version = 0
        self._conn: AsyncConnection | None = None
        self._lock = asyncio.Lock()

    async def _ensure_listening(self) -> None:
        if self._conn is not None:
            return
        async with self._lock:
            if self._conn is not None:
                return
            conn = await self.db.engine.connect()
            raw = await conn.get_raw_connection()
            await raw.driver_connection.add_listener(CHANNEL, self._on_notify)  # type: ignore[union-attr]
            self._conn = conn

    def _on_notify(self, *_: Any) -> None:
        async def bump() -> None:
            async with self._cond:
                self._version += 1
                self._cond.notify_all()

        asyncio.get_running_loop().create_task(bump())

    async def serve(
        self, fetch: Callable[[AsyncConnection], Awaitable[dict[str, Any] | None]], wait_s: float
    ) -> dict[str, Any] | None:
        """fetch 在系统上下文中生成并读取快照；没有新快照且 wait_s > 0 时等待变更通知。"""
        wait_s = min(max(wait_s, 0.0), MAX_WAIT_S)
        if wait_s > 0:
            await self._ensure_listening()
        seen = self._version
        async with self.db.tx(DbScope.system()) as conn:
            snap = await fetch(conn)
        if snap is not None or wait_s == 0:
            return snap
        async with self._cond:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._cond.wait_for(lambda: self._version != seen), wait_s)
        async with self.db.tx(DbScope.system()) as conn:
            return await fetch(conn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
