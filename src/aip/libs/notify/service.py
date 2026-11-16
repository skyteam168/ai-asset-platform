"""Notifier：待办 / 风险 / 确认到期经 outbox 投递（邮件 + IM）。

出站 HTTP（IM webhook）必须经 libs/httpclient；邮件默认写日志（可注入 SMTP 实现）。
密钥与正文不含明文凭据。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aip.libs.common.config import Settings
from aip.libs.common.ids import new_id
from aip.libs.httpclient import ConnectionConfig, build_client
from aip.libs.httpclient.config import Scheme

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8


class Channel(Protocol):
    name: str

    async def send(self, recipient: str, subject: str, body: str) -> None: ...


def _parse_recipients(recipient: str) -> tuple[list[str], list[str]]:
    """支持普通邮箱，或 JSON：{"to":["a@x"],"cc":["b@y"]}。"""
    raw = recipient.strip()
    if raw.startswith("{"):
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return [raw], []
        if isinstance(doc, dict):
            to = [str(x).strip() for x in (doc.get("to") or []) if str(x).strip()]
            cc = [str(x).strip() for x in (doc.get("cc") or []) if str(x).strip()]
            return to or [raw], cc
    return [raw], []


class LogMailChannel:
    """开发默认：只记日志，不真发邮件。"""

    name = "email"

    async def send(self, recipient: str, subject: str, body: str) -> None:
        to, cc = _parse_recipients(recipient)
        log.info(
            "notify.email to=%s cc=%s subject=%s bytes=%s",
            ",".join(to),
            ",".join(cc) or "-",
            subject,
            len(body),
        )


class SmtpMailChannel:
    """生产 SMTP（stdlib smtplib，经 asyncio.to_thread；不落明文密码到日志）。"""

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        user: str = "",
        password: str = "",
        mail_from: str = "",
        starttls: bool = True,
    ) -> None:
        if not host:
            raise ValueError("smtp host required")
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.mail_from = mail_from or user
        self.starttls = starttls

    async def send(self, recipient: str, subject: str, body: str) -> None:
        import asyncio
        import smtplib
        from email.message import EmailMessage

        to, cc = _parse_recipients(recipient)
        if not to:
            raise ValueError("smtp recipient required")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.mail_from
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg.set_content(body)

        def _send() -> None:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                if self.starttls:
                    smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)

        last: BaseException | None = None
        for attempt in range(3):
            try:
                await asyncio.to_thread(_send)
                last = None
                break
            except Exception as e:
                last = e
                # 不把 password / 连接串写入日志
                log.warning(
                    "notify.smtp_retry attempt=%s to=%s err=%s",
                    attempt + 1,
                    ",".join(to),
                    type(e).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(0.4 * (attempt + 1))
        if last is not None:
            raise last
        log.info("notify.smtp to=%s cc=%s subject=%s bytes=%s", ",".join(to), ",".join(cc) or "-", subject, len(body))


def _feishu_sign(secret: str, timestamp: str) -> str:
    """飞书自定义机器人签名：HMAC-SHA256(timestamp\\nsecret) → base64。"""
    import base64
    import hashlib
    import hmac

    string_to_sign = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class FeishuChannel:
    """飞书自定义机器人：msg_type=text（经 httpclient；可选签名密钥）。"""

    name = "im"

    def __init__(self, webhook_url: str, *, secret: str = "") -> None:
        self.webhook_url = webhook_url
        self.secret = secret

    async def send(self, recipient: str, subject: str, body: str) -> None:
        parsed = urlparse(self.webhook_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid feishu webhook url")
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        scheme: Scheme = "https" if parsed.scheme == "https" else "http"
        text_body = f"[{subject}] {body}" if subject else body
        if recipient:
            text_body = f"@{recipient} {text_body}"
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text_body[:4000]}}
        if self.secret:
            ts = str(int(dt.datetime.now(dt.UTC).timestamp()))
            payload["timestamp"] = ts
            payload["sign"] = _feishu_sign(self.secret, ts)
        cfg = ConnectionConfig(
            id="notify-feishu",
            base_url=base,
            scheme=scheme,
            tls_mode="system_ca" if scheme == "https" else None,
            transport_exception_reason="notify Feishu webhook" if scheme == "http" else None,
            transport_exception_expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC) if scheme == "http" else None,
        )
        try:
            async with build_client(cfg) as client:
                r = await client.post(path, json=payload)
                r.raise_for_status()
        except Exception as e:
            log.warning("notify.feishu_failed err=%s", type(e).__name__)
            raise


class ImWebhookChannel:
    """IM：对配置的 webhook URL POST JSON（经 httpclient）。"""

    name = "im"

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    async def send(self, recipient: str, subject: str, body: str) -> None:
        parsed = urlparse(self.webhook_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid im webhook url")
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        scheme: Scheme = "https" if parsed.scheme == "https" else "http"
        # http 为传输例外：与 webhook 内网回调同策略（architecture.md 9.6）
        cfg = ConnectionConfig(
            id="notify-im",
            base_url=base,
            scheme=scheme,
            tls_mode="system_ca" if scheme == "https" else None,
            transport_exception_reason="notify IM webhook" if scheme == "http" else None,
            transport_exception_expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC) if scheme == "http" else None,
        )
        async with build_client(cfg) as client:
            r = await client.post(path, json={"to": recipient, "subject": subject, "body": body})
            r.raise_for_status()


class Notifier:
    def __init__(self, channels: list[Channel] | None = None) -> None:
        self.channels = {c.name: c for c in (channels or [LogMailChannel()])}


async def enqueue(
    conn: AsyncConnection,
    *,
    company_id: str | uuid.UUID,
    channel: str,
    recipient: str,
    subject: str,
    body: str,
    kind: str,
    ref: dict[str, Any] | None = None,
) -> uuid.UUID:
    mid = new_id()
    await conn.execute(
        text(
            """INSERT INTO notify.channel_outbox
               (id, company_id, channel, recipient, subject, body, kind, ref, status, next_attempt_at)
               VALUES (:id, :c, :ch, :r, :s, :b, :k, CAST(:ref AS jsonb), 'pending', now())"""
        ),
        {
            "id": mid,
            "c": str(company_id),
            "ch": channel,
            "r": recipient,
            "s": subject[:500],
            "b": body,
            "k": kind,
            "ref": json.dumps(ref or {}, ensure_ascii=False),
        },
    )
    return mid


async def enqueue_channels(
    conn: AsyncConnection,
    *,
    company_id: str | uuid.UUID,
    recipient: str,
    subject: str,
    body: str,
    kind: str,
    ref: dict[str, Any] | None = None,
    im_recipient: str | None = None,
    im_enabled: bool = False,
) -> list[uuid.UUID]:
    """同时入队 email；若配置了 IM Webhook 再入队 im（收件人缺省用 email）。"""
    ids = [
        await enqueue(
            conn,
            company_id=company_id,
            channel="email",
            recipient=recipient,
            subject=subject,
            body=body,
            kind=kind,
            ref=ref,
        )
    ]
    if im_enabled:
        ids.append(
            await enqueue(
                conn,
                company_id=company_id,
                channel="im",
                recipient=(im_recipient or recipient),
                subject=subject,
                body=body,
                kind=kind,
                ref=ref,
            )
        )
    return ids


def build_notifier(
    *,
    im_webhook_url: str | None = None,
    feishu_webhook_url: str | None = None,
    feishu_secret: str | None = None,
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_password: str = "",
    smtp_from: str = "",
    smtp_starttls: bool = True,
) -> Notifier:
    """装配通知通道：有 SMTP 主机用 SmtpMail，否则 LogMail；飞书优先于通用 IM Webhook。"""
    channels: list[Channel] = []
    if smtp_host:
        channels.append(
            SmtpMailChannel(
                host=smtp_host,
                port=smtp_port,
                user=smtp_user,
                password=smtp_password,
                mail_from=smtp_from or smtp_user,
                starttls=smtp_starttls,
            )
        )
    else:
        channels.append(LogMailChannel())
    if feishu_webhook_url:
        channels.append(FeishuChannel(feishu_webhook_url, secret=feishu_secret or ""))
    elif im_webhook_url:
        channels.append(ImWebhookChannel(im_webhook_url))
    return Notifier(channels)


def build_notifier_from_settings(settings: Settings) -> Notifier:
    """同步装配：仅环境变量（启动占位；生产投递请用 build_notifier_resolved）。"""
    return build_notifier(
        im_webhook_url=settings.notify_im_webhook_url,
        feishu_webhook_url=settings.notify_feishu_webhook_url,
        feishu_secret=settings.notify_feishu_secret,
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_user=settings.smtp_user,
        smtp_password=settings.smtp_password,
        smtp_from=settings.smtp_from,
        smtp_starttls=settings.smtp_starttls,
    )


async def build_notifier_resolved(
    settings: Settings,
    store: Any | None = None,
) -> Notifier:
    """控制台邮件配置优先（启用且有 host）；否则 AIP_SMTP_*；密码来自 OpenBao 或环境变量。"""
    from aip.libs.notify import mail_settings as ms

    meta = ms.load_meta(settings)
    host = ""
    port = settings.smtp_port
    user = settings.smtp_user
    password = settings.smtp_password
    mail_from = settings.smtp_from
    starttls = settings.smtp_starttls
    if meta.get("enabled") and str(meta.get("host") or "").strip():
        host = str(meta["host"]).strip()
        port = int(meta.get("port") or 587)
        user = str(meta.get("user") or "")
        mail_from = str(meta.get("mail_from") or user)
        starttls = bool(meta.get("starttls", True))
        if store is not None:
            password = await ms.resolve_password(store, settings)
        else:
            password = settings.smtp_password
    elif settings.smtp_host:
        host = settings.smtp_host
    return build_notifier(
        im_webhook_url=settings.notify_im_webhook_url,
        feishu_webhook_url=settings.notify_feishu_webhook_url,
        feishu_secret=settings.notify_feishu_secret,
        smtp_host=host,
        smtp_port=port,
        smtp_user=user,
        smtp_password=password,
        smtp_from=mail_from,
        smtp_starttls=starttls,
    )


async def flush_forever(
    engine: AsyncEngine,
    notifier: Notifier | Any,
    interval_s: float,
    *,
    resolve: Any | None = None,
) -> None:
    """后台循环 flush outbox（对齐 webhook deliver_forever）。

    resolve：可选异步工厂，每轮重建 Notifier（便于控制台改 SMTP 后无需重启）。
    """
    import asyncio

    while True:
        try:
            n = await resolve() if resolve is not None else notifier
            await flush_outbox(engine, n)
        except Exception:  # 单轮失败不退出循环
            log.exception("notify flush_forever failed")
        await asyncio.sleep(interval_s)


async def flush_outbox(engine: AsyncEngine, notifier: Notifier, *, limit: int = 50) -> int:
    """投递 pending 消息；成功标记 sent，失败退避，超过上限 dead。"""
    sent = 0
    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
        rows = (
            (
                await conn.execute(
                    text(
                        """SELECT id, channel, recipient, subject, body, attempts FROM notify.channel_outbox
                           WHERE status = 'pending' AND next_attempt_at <= now()
                           ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT :lim"""
                    ),
                    {"lim": limit},
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            ch = notifier.channels.get(row["channel"])
            try:
                if ch is None:
                    raise RuntimeError(f"unknown channel {row['channel']}")
                await ch.send(row["recipient"], row["subject"], row["body"])
                await conn.execute(
                    text(
                        "UPDATE notify.channel_outbox SET status = 'sent', sent_at = now(), last_error = NULL "
                        "WHERE id = :id"
                    ),
                    {"id": row["id"]},
                )
                sent += 1
            except Exception as e:  # 单条失败不阻断批次
                attempts = int(row["attempts"]) + 1
                status = "dead" if attempts >= MAX_ATTEMPTS else "pending"
                backoff = min(3600, 2**attempts)
                await conn.execute(
                    text(
                        """UPDATE notify.channel_outbox SET status = :s, attempts = :a, last_error = :e,
                             next_attempt_at = now() + make_interval(secs => :b) WHERE id = :id"""
                    ),
                    {
                        "id": row["id"],
                        "s": status,
                        "a": attempts,
                        "e": str(e)[:500],
                        "b": backoff,
                    },
                )
                log.warning("notify flush failed id=%s: %s", row["id"], e)
    return sent
