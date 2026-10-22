"""通知插件（architecture.md 8.5 Notifier：邮件 + IM）。"""

from aip.libs.notify.service import (
    ImWebhookChannel,
    LogMailChannel,
    Notifier,
    build_notifier,
    enqueue,
    enqueue_channels,
    flush_forever,
    flush_outbox,
)

__all__ = [
    "ImWebhookChannel",
    "LogMailChannel",
    "Notifier",
    "build_notifier",
    "enqueue",
    "enqueue_channels",
    "flush_forever",
    "flush_outbox",
]
