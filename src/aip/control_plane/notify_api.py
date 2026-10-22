"""Notifier 运维接口（H43）：手动 flush outbox。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aip.control_plane.iam.api import ActorDep, DbDep
from aip.libs.common.config import get_settings
from aip.libs.notify.service import build_notifier_from_settings, flush_outbox

V = "/api/v1"


class NotifyFlushResult(BaseModel):
    sent: int = Field(ge=0)


def router() -> APIRouter:
    r = APIRouter()

    @r.post(
        f"{V}/notify/flush",
        tags=["notify"],
        operation_id="notifyFlush",
        response_model=NotifyFlushResult,
    )
    async def notify_flush(actor: ActorDep, db: DbDep) -> NotifyFlushResult:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以触发通知投递")
        settings = get_settings()
        n = build_notifier_from_settings(settings)
        sent = await flush_outbox(db.engine, n)
        return NotifyFlushResult(sent=sent)

    return r
