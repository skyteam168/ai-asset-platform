"""Notifier：待办 / 风险 / 确认到期经 outbox 投递（邮件 + IM）。

出站 HTTP（IM webhook）必须经 libs/httpclient；邮件默认写日志（可注入 SMTP 实现）。
密钥与正文不含明文凭据。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aip.libs.common.ids import new_id
from aip.libs.httpclient import ConnectionConfig, build_client
from aip.libs.httpclient.config import Scheme

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8


class Channel(Protocol):
    name: str

    async def send(self, recipient: str, subject: str, body: str) -> None: ...


class LogMailChannel:
    """开发默认：只记日志，不真发邮件。"""

    name = "email"

    async def send(self, recipient: str, subject: str, body: str) -> None:
        log.info("notify.email to=%s subject=%s bytes=%s", recipient, subject, len(body))


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
        cfg = ConnectionConfig(
            id="notify-im",
            base_url=base,
            scheme=scheme,
            tls_mode="system_ca" if scheme == "https" else None,
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
