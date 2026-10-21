"""风控接口：规则、事件、确认关闭、申请复核恢复、手动扫描（architecture.md 12.4）。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.risk import schemas as out
from aip.control_plane.risk import service as risk
from aip.libs.common.paging import Page

V = "/api/v1"
log = logging.getLogger(__name__)


class RulePatch(BaseModel):
    params: dict[str, Any] | None = None
    enabled: bool | None = None
    action: Literal["alert", "suspend_asset"] | None = None
    severity: Literal["low", "medium", "high"] | None = None


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=1000)


def router() -> APIRouter:
    r = APIRouter()

    @r.get(f"{V}/risk-rules", tags=["risk"], operation_id="listRiskRules", response_model=Page[out.RiskRule])
    async def rules(actor: ActorDep, db: DbDep) -> Page[out.RiskRule]:
        actor.require(actor.is_global or actor.has("company_admin"), "需要平台角色")
        async with db.tx(actor.db_scope()) as conn:
            items = [out.RiskRule.model_validate(x) for x in await risk.list_rules(conn)]
        return Page(items=items)

    @r.put(f"{V}/risk-rules/{{code}}", tags=["risk"], operation_id="updateRiskRule", response_model=out.RiskRule)
    async def update(code: str, body: RulePatch, actor: ActorDep, db: DbDep) -> out.RiskRule:
        async with db.tx(actor.db_scope()) as conn:
            return out.RiskRule.model_validate(
                await risk.update_rule(conn, actor, code, body.model_dump(exclude_none=True))
            )

    @r.get(f"{V}/risk-events", tags=["risk"], operation_id="listRiskEvents", response_model=Page[out.RiskEvent])
    async def events(
        actor: ActorDep, db: DbDep, status: str | None = None, limit: int = 200
    ) -> Page[out.RiskEvent]:
        actor.require(
            actor.is_global or actor.has("company_admin") or actor.has("data_owner"),
            "需要平台管理员、数据 Owner 或审计员",
        )
        async with db.tx(actor.db_scope()) as conn:
            items = [out.RiskEvent.model_validate(x) for x in await risk.list_events(conn, status, limit)]
        return Page(items=items)

    @r.post(
        f"{V}/risk-events/{{id}}/acknowledge",
        tags=["risk"],
        operation_id="acknowledgeRiskEvent",
        response_model=out.RiskEvent,
    )
    async def ack(id: uuid.UUID, body: NoteIn, actor: ActorDep, db: DbDep) -> out.RiskEvent:
        async with db.tx(actor.db_scope()) as conn:
            return out.RiskEvent.model_validate(await risk.acknowledge(conn, actor, id, body.note))

    @r.post(
        f"{V}/risk-events/{{id}}/request-resume",
        tags=["risk"],
        operation_id="requestRiskResume",
        response_model=out.RiskResumeRequested,
    )
    async def resume(id: uuid.UUID, body: NoteIn, actor: ActorDep, db: DbDep) -> out.RiskResumeRequested:
        async with db.tx(actor.db_scope()) as conn:
            return out.RiskResumeRequested.model_validate(await risk.request_resume(conn, actor, id, body.note))

    @r.post(f"{V}/risk-scan", tags=["risk"], operation_id="runRiskScan", response_model=out.RiskScanResult)
    async def scan_now(actor: ActorDep, db: DbDep) -> out.RiskScanResult:
        """平台管理员手动触发一次扫描（演练与排障用；平时由后台定时扫描）。"""
        actor.require(actor.has("platform_admin"), "只有平台管理员可以手动扫描")
        async with db.tx(DbScope.system()) as conn:
            created = [out.RiskEvent.model_validate(x) for x in await risk.scan(conn)]
        return out.RiskScanResult(created=created)

    return r


async def scan_forever(db: Database, interval_s: float) -> None:
    """后台定时扫描；多副本同时扫描也安全（事件唯一约束保证每个窗口只处置一次）。"""
    while True:
        try:
            async with db.tx(DbScope.system()) as conn:
                await risk.scan(conn)
        except Exception:
            log.exception("risk scan failed")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(interval_s)
