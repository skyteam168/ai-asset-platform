"""Webhook 管理接口与后台投递循环。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.risk import service as risk
from aip.control_plane.webhook import service as wh

V = "/api/v1"
log = logging.getLogger(__name__)


class SubscribeIn(BaseModel):
    events: list[str] = Field(min_length=1)
    url: str = Field(min_length=8, max_length=2048)


class StatusIn(BaseModel):
    status: Literal["active", "disabled"]


def router() -> APIRouter:
    r = APIRouter()

    @r.post(f"{V}/consumers/{{id}}/webhooks", tags=["webhook"], operation_id="createWebhook", status_code=201)
    async def create(id: uuid.UUID, body: SubscribeIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await wh.create_subscription(
                conn, actor, consumer_id=id, events=body.events, url=body.url
            )

    @r.get(f"{V}/consumers/{{id}}/webhooks", tags=["webhook"], operation_id="listWebhooks")
    async def list_wh(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return {"items": await wh.list_endpoints(conn, actor, id)}

    @r.post(
        f"{V}/webhooks/{{id}}/rotate-secret",
        tags=["webhook"],
        operation_id="rotateWebhookSecret",
    )
    async def rotate(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await wh.rotate_secret(conn, actor, id)

    @r.put(f"{V}/webhooks/{{id}}/status", tags=["webhook"], operation_id="setWebhookStatus")
    async def set_status(id: uuid.UUID, body: StatusIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            await wh.set_status(conn, actor, id, body.status)
        return {"id": str(id), "status": body.status}

    @r.get(f"{V}/webhooks/{{id}}/deliveries", tags=["webhook"], operation_id="listWebhookDeliveries")
    async def deliveries(id: uuid.UUID, actor: ActorDep, db: DbDep, limit: int = 50) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return {"items": await wh.list_deliveries(conn, actor, id, limit=limit)}

    @r.post(f"{V}/webhooks/process", tags=["webhook"], operation_id="processWebhooks")
    async def process(actor: ActorDep, db: DbDep) -> dict[str, Any]:
        """平台管理员手动触发一次到期投递（演练与排障；平时由后台循环处理）。"""
        actor.require(actor.has("platform_admin"), "只有平台管理员可以手动投递")
        async with db.tx(DbScope.system()) as conn:
            return {"results": await wh.process_due(conn)}

    return r


def wire_risk_notifier(db: Database) -> None:
    """风险规则命中后入队 risk.triggered webhook（独立事务，失败不影响熔断处置）。"""

    async def _notify(event: dict[str, Any]) -> None:
        company = event.get("company_id")
        company_id = uuid.UUID(company) if isinstance(company, str) else company
        try:
            async with db.tx(DbScope.system()) as conn:
                ids = await wh.enqueue(
                    conn,
                    event_kind="risk.triggered",
                    body={"event": "risk.triggered", "payload": event},
                    company_id=company_id,
                    event_id=str(event.get("id") or ""),
                )
            if ids:
                async with db.tx(DbScope.system()) as conn:
                    for did in ids:
                        await wh.deliver_one(conn, did)
        except Exception:
            log.exception("risk webhook enqueue failed")

    risk.register_notifier(_notify)


async def deliver_forever(db: Database, interval_s: float) -> None:
    """后台周期处理到期投递。"""
    while True:
        try:
            async with db.tx(DbScope.system()) as conn:
                await wh.process_due(conn)
        except Exception:
            log.exception("webhook deliver loop failed")
        await asyncio.sleep(interval_s)
