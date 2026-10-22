"""通知插件（architecture.md 8.5 Notifier：邮件 + IM）。"""

from aip.libs.notify.service import (
    FeishuChannel,
    ImWebhookChannel,
    LogMailChannel,
    Notifier,
    SmtpMailChannel,
    build_notifier,
    build_notifier_from_settings,
    enqueue,
    enqueue_channels,
    flush_forever,
    flush_outbox,
)

__all__ = [
    "FeishuChannel",
    "ImWebhookChannel",
    "LogMailChannel",
    "Notifier",
    "SmtpMailChannel",
    "build_notifier",
    "build_notifier_from_settings",
    "enqueue",
    "enqueue_channels",
    "flush_forever",
    "flush_outbox",
]
