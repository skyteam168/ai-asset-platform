"""H56：SMTP / 飞书通知通道骨架。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from aip.libs.notify import FeishuChannel, SmtpMailChannel, build_notifier


def test_build_notifier_uses_smtp_when_host_set() -> None:
    n = build_notifier(smtp_host="smtp.example.internal", smtp_from="aip@example.com")
    assert isinstance(n.channels["email"], SmtpMailChannel)


def test_build_notifier_prefers_feishu_over_im() -> None:
    n = build_notifier(
        im_webhook_url="http://127.0.0.1/im",
        feishu_webhook_url="http://127.0.0.1/feishu",
    )
    assert isinstance(n.channels["im"], FeishuChannel)


@pytest.mark.asyncio
async def test_smtp_send_calls_smtplib() -> None:
    ch = SmtpMailChannel(host="smtp.test", port=587, user="u", password="p", mail_from="f@t")
    with patch("smtplib.SMTP") as smtp_cls:
        smtp = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp
        await ch.send("to@t", "subj", "body")
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("u", "p")
        smtp.send_message.assert_called_once()


@pytest.fixture
async def feishu_server() -> Any:
    payloads: list[dict[str, Any]] = []

    async def handle(request: Request) -> Response:
        payloads.append(await request.json())
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/feishu", handle, methods=["POST"])])
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield payloads, f"http://127.0.0.1:{port}/feishu"
    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_feishu_payload_shape(feishu_server: Any) -> None:
    payloads, url = feishu_server
    await FeishuChannel(url).send("ops", "告警", "磁盘满")
    assert len(payloads) == 1
    assert payloads[0]["msg_type"] == "text"
    assert "磁盘满" in payloads[0]["content"]["text"]
    assert "@ops" in payloads[0]["content"]["text"]
