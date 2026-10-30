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
    ch = SmtpMailChannel(host="smtp.test", port=587, user="u", password="secret-pw-xyz", mail_from="f@t")
    with patch("smtplib.SMTP") as smtp_cls:
        smtp = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp
        await ch.send("to@t", "subj", "body")
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("u", "secret-pw-xyz")
        smtp.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_smtp_retries_then_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    """G4.1：失败重试；日志不含明文密码。"""
    import logging

    ch = SmtpMailChannel(host="smtp.test", port=587, user="u", password="secret-pw-xyz", mail_from="f@t")
    with patch("smtplib.SMTP") as smtp_cls:
        smtp = MagicMock()
        smtp_cls.return_value.__enter__.return_value = smtp
        smtp.send_message.side_effect = [OSError("temp"), None]
        with caplog.at_level(logging.WARNING, logger="aip.libs.notify.service"):
            await ch.send("to@t", "subj", "body")
        assert smtp.send_message.call_count == 2
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "secret-pw-xyz" not in joined
    assert "notify.smtp_retry" in joined


@pytest.mark.smtp
@pytest.mark.asyncio
async def test_live_smtp_send() -> None:
    """真发一封（需 AIP_TEST_SMTP_*）；无配置则 skip。"""
    import os

    host = os.environ.get("AIP_TEST_SMTP_HOST", "").strip()
    to = os.environ.get("AIP_TEST_SMTP_TO", "").strip()
    if not host or not to:
        pytest.skip("AIP_TEST_SMTP_HOST / AIP_TEST_SMTP_TO not set")
    ch = SmtpMailChannel(
        host=host,
        port=int(os.environ.get("AIP_TEST_SMTP_PORT", "587")),
        user=os.environ.get("AIP_TEST_SMTP_USER", ""),
        password=os.environ.get("AIP_TEST_SMTP_PASSWORD", ""),
        mail_from=os.environ.get("AIP_TEST_SMTP_FROM") or os.environ.get("AIP_TEST_SMTP_USER", ""),
        starttls=os.environ.get("AIP_TEST_SMTP_STARTTLS", "1") not in ("0", "false", "False"),
    )
    await ch.send(to, "[AIP] staging SMTP sample", "G4.1 notify.smtp live sample; no secrets in body.")


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


@pytest.mark.asyncio
async def test_feishu_signed_payload(feishu_server: Any) -> None:
    """G4.2：配置 secret 时 payload 含 timestamp + sign。"""
    payloads, url = feishu_server
    await FeishuChannel(url, secret="sec").send("ops", "s", "b")
    assert "timestamp" in payloads[-1] and "sign" in payloads[-1]


@pytest.mark.asyncio
async def test_feishu_bad_url_does_not_crash_loop() -> None:
    """错误 URL 抛业务异常，不拖垮调用方。"""
    with pytest.raises(ValueError):
        await FeishuChannel("not-a-url").send("x", "s", "b")


@pytest.mark.feishu
@pytest.mark.asyncio
async def test_live_feishu_webhook() -> None:
    import os

    url = os.environ.get("AIP_TEST_FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        pytest.skip("AIP_TEST_FEISHU_WEBHOOK_URL not set")
    secret = os.environ.get("AIP_TEST_FEISHU_SECRET", "")
    await FeishuChannel(url, secret=secret).send("ops", "[AIP] staging", "G4.2 feishu live sample")
