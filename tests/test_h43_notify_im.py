"""H43：IM Webhook 入队 + flush；未配置时只发邮件。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from aip.libs.notify import ImWebhookChannel, LogMailChannel, Notifier, enqueue, enqueue_channels, flush_outbox
from iam_seed import Seed, app_url

pytestmark = pytest.mark.infra


class _MockIm:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.fail_once = False

    async def handle(self, request: Request) -> Response:
        body = await request.json()
        self.payloads.append(body)
        if self.fail_once:
            self.fail_once = False
            return Response(status_code=500)
        return JSONResponse({"ok": True})


@pytest.fixture
async def im_server() -> Any:
    mock = _MockIm()
    app = Starlette(routes=[Route("/hook", mock.handle, methods=["POST"])])
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    yield mock, f"http://127.0.0.1:{port}/hook"
    server.should_exit = True
    await task


async def test_im_enqueue_flush_sent(seeded: Seed, im_server: Any) -> None:
    mock, url = im_server
    engine = create_async_engine(app_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            ids = await enqueue_channels(
                conn,
                company_id=seeded.vn,
                recipient="ops@example.com",
                subject="IM 测",
                body="hello",
                kind="human_task",
                im_enabled=True,
            )
        assert len(ids) == 2
        n = Notifier([LogMailChannel(), ImWebhookChannel(url)])
        assert await flush_outbox(engine, n) == 2
        assert len(mock.payloads) == 1
        assert mock.payloads[0]["to"] == "ops@example.com"
        assert mock.payloads[0]["subject"] == "IM 测"
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            rows = (
                await conn.execute(
                    text("SELECT channel, status FROM notify.channel_outbox WHERE id = ANY(:ids)"),
                    {"ids": ids},
                )
            ).mappings().all()
        assert {r["channel"]: r["status"] for r in rows} == {"email": "sent", "im": "sent"}
    finally:
        await engine.dispose()


async def test_im_failure_backoff(seeded: Seed, im_server: Any) -> None:
    mock, url = im_server
    mock.fail_once = True
    engine = create_async_engine(app_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            mid = await enqueue(
                conn,
                company_id=seeded.vn,
                channel="im",
                recipient="u1",
                subject="fail",
                body="x",
                kind="approval",
            )
        n = Notifier([ImWebhookChannel(url)])
        assert await flush_outbox(engine, n) == 0
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            row = (
                await conn.execute(
                    text("SELECT status, attempts FROM notify.channel_outbox WHERE id = :id"),
                    {"id": mid},
                )
            ).mappings().one()
        assert row["status"] == "pending"
        assert int(row["attempts"]) == 1
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            await conn.execute(
                text("UPDATE notify.channel_outbox SET next_attempt_at = now() WHERE id = :id"),
                {"id": mid},
            )
        assert await flush_outbox(engine, n) == 1
    finally:
        await engine.dispose()


async def test_no_im_when_disabled(seeded: Seed) -> None:
    engine = create_async_engine(app_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            ids = await enqueue_channels(
                conn,
                company_id=seeded.vn,
                recipient="a@b.c",
                subject="only mail",
                body="b",
                kind="human_task",
                im_enabled=False,
            )
        assert len(ids) == 1
        n = Notifier([LogMailChannel()])
        assert await flush_outbox(engine, n) == 1
    finally:
        await engine.dispose()
