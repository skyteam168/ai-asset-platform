"""通知插件（architecture.md 8.5 Notifier：邮件 + IM）。"""

from aip.libs.notify.service import LogMailChannel, Notifier, enqueue, flush_outbox

__all__ = ["LogMailChannel", "Notifier", "enqueue", "flush_outbox"]
