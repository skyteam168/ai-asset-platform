"""数据面 → 控制面事件回传（服务间认证）：证书指纹、传输例外到期、按工号匹配的绑定回写。"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header
from pydantic import BaseModel
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.conn import service as conn_service
from aip.control_plane.db import DbScope
from aip.control_plane.iam import tables as iam_t
from aip.control_plane.iam.api import DbDep, _bearer
from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.events import emit
from aip.control_plane.registry.service import notify_change
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow

log = logging.getLogger("aip.internal_events")
BINDING_DAYS = 365


class Event(BaseModel):
    kind: str
    data: dict[str, Any]


class EventsIn(BaseModel):
    instance_id: str
    events: list[Event]


async def apply(conn: AsyncConnection, ev: Event) -> str:
    d = ev.data
    sys_ = Actor.system_actor()
    if ev.kind in (
        "tls.fingerprint_seen",
        "tls.fingerprint_changed",
        "transport_exception.expired",
        "tls.cert_expired",
        "tls.cert_expiring",
    ):
        try:
            cid = uuid.UUID(str(d["connection_id"]))
            row = await conn_service.row(conn, cid)
        except (ValueError, KeyError, PlatformError):
            return "ignored"
        if ev.kind == "tls.fingerprint_seen" and not row["tls_last_seen_fingerprint"]:
            await conn.execute(
                update(conn_service.system_connection)
                .where(conn_service.system_connection.c.id == cid)
                .values(tls_last_seen_fingerprint=d["fingerprint"])
            )
            return "recorded"
        if ev.kind == "transport_exception.expired":
            await conn.execute(
                update(conn_service.system_connection)
                .where(conn_service.system_connection.c.id == cid)
                .values(status="disabled", updated_at=utcnow())
            )
        # 指纹变化、证书过期都作为风险事件留痕，并触发新的发布快照
        await notify_change(conn, f"connection.{ev.kind}", row["company_id"], cid, sys_, d)
        log.warning("connection event %s connection=%s", ev.kind, cid)
        return "applied"
    if ev.kind == "binding.employee_no_matched":
        consumer_id, user_id = uuid.UUID(d["consumer_id"]), uuid.UUID(d["user_id"])
        exists = (
            await conn.execute(
                select(iam_t.user_binding.c.id).where(
                    and_(
                        iam_t.user_binding.c.consumer_id == consumer_id,
                        iam_t.user_binding.c.external_subject == d["external_subject"],
                    )
                )
            )
        ).first()
        if exists:
            return "exists"
        company = (
            await conn.execute(select(iam_t.consumer.c.company_id).where(iam_t.consumer.c.id == consumer_id))
        ).scalar_one()
        bid = new_id()
        await conn.execute(
            iam_t.user_binding.insert().values(
                id=bid,
                consumer_id=consumer_id,
                external_subject=d["external_subject"],
                user_id=user_id,
                company_id=company,
                created_by=None,
                expires_at=utcnow() + dt.timedelta(days=BINDING_DAYS),
            )
        )
        await emit(conn, "binding.created", company, bid, None, {"source": "by_employee_no"})
        return "created"
    return "ignored"


def router() -> APIRouter:
    r = APIRouter(tags=["internal"])

    @r.post("/api/v1/internal/events", operation_id="ingestDataPlaneEvents")
    async def ingest(
        body: EventsIn, db: DbDep, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        if not secrets.compare_digest(_bearer(authorization), get_settings().internal_service_token):
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "需要服务凭据")
        results = []
        async with db.tx(DbScope.system()) as conn:
            for ev in body.events:
                results.append(await apply(conn, ev))
        return {"results": results}

    return r
