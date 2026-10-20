"""M6e：Notifier outbox（邮件日志通道）。"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aip.libs.notify import LogMailChannel, Notifier, enqueue, flush_outbox
from iam_seed import Seed, app_url

pytestmark = pytest.mark.infra


async def test_enqueue_and_flush(seeded: Seed) -> None:
    engine = create_async_engine(app_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            mid = await enqueue(
                conn,
                company_id=seeded.vn,
                channel="email",
                recipient="ops@example.com",
                subject="待办提醒",
                body="请处理人工任务",
                kind="human_task",
                ref={"task_id": "t1"},
            )
        n = Notifier([LogMailChannel()])
        assert await flush_outbox(engine, n) == 1
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            status = (
                await conn.execute(text("SELECT status FROM notify.channel_outbox WHERE id = :id"), {"id": mid})
            ).scalar_one()
        assert status == "sent"
    finally:
        await engine.dispose()
