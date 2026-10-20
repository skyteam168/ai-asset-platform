"""门禁、审批、使用申请、复核活动接口（docs/api/control-plane.openapi.yaml）。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.connectors.credentials import OpenBaoSecretStore, SecretStore
from aip.control_plane.approval import service as approval
from aip.control_plane.gates import service as gates
from aip.control_plane.grant import flows
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.libs.common.config import get_settings

V = "/api/v1"


class CaseIn(BaseModel):
    name: str
    kind: Literal["functional", "permission"]
    args: dict[str, Any]
    expect: dict[str, Any]
    principal: dict[str, Any] = Field(default_factory=dict)
    critical: bool = True


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = ""
    results: dict[str, Any] | None = None
    on_behalf_of: uuid.UUID | None = None


class DelegateIn(BaseModel):
    delegate_user_id: uuid.UUID
    valid_to: dt.datetime


class RolloutIn(BaseModel):
    env: Literal["test", "staging", "prod"]
    scope: dict[str, Any]
    exposure: list[Literal["rest", "mcp", "a2a"]] = ["rest"]
    min_calls: int = Field(default=20, ge=0)
    max_error_rate: float = Field(default=0.05, ge=0, le=1)


class UsageIn(BaseModel):
    consumer_id: uuid.UUID
    asset_id: uuid.UUID
    env: Literal["test", "staging", "prod"]
    reason: str = Field(min_length=4)


class ReviewIn(BaseModel):
    company_id: uuid.UUID
    expiring_days: int = 30
    unused_days: int = 90


def _secrets(request: Request) -> SecretStore:
    s: SecretStore | None = getattr(request.app.state, "secrets", None)
    if s is None:
        settings = get_settings()
        s = OpenBaoSecretStore(settings.openbao_addr, settings.openbao_token)
    return s


def _str(v: Any) -> Any:
    return str(v) if isinstance(v, uuid.UUID | dt.datetime) else v


async def _subject(conn: AsyncConnection, row: dict[str, Any]) -> str:
    """审批单的可读摘要：门禁审批显示资产名与版本，使用申请显示资产名与环境。"""
    from aip.control_plane.registry import tables as rt

    if row["kind"] in ("G5", "G6"):
        r = (
            await conn.execute(
                select(rt.asset.c.name, rt.asset_version.c.semver)
                .select_from(rt.asset_version.join(rt.asset, rt.asset.c.id == rt.asset_version.c.asset_id))
                .where(rt.asset_version.c.id == row["subject_id"])
            )
        ).first()
        return f"{r.name}@{r.semver}" if r else str(row["subject_id"])
    if row["kind"] == "usage":
        asset_id = row["payload"].get("asset_id")
        name = (
            (await conn.execute(select(rt.asset.c.name).where(rt.asset.c.id == uuid.UUID(asset_id)))).scalar()
            if asset_id
            else None
        )
        return f"{name or asset_id}（{row['payload'].get('env', '')}）"
    return str(row["subject_id"])


def router() -> APIRouter:
    r = APIRouter()

    @r.post(f"{V}/versions/{{id}}/test-cases", tags=["release"], operation_id="addTestCase", status_code=201)
    async def add_case(id: uuid.UUID, body: CaseIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            cid = await gates.add_test_case(
                conn,
                actor,
                id,
                name=body.name,
                kind=body.kind,
                args=body.args,
                expect=body.expect,
                principal=body.principal,
                critical=body.critical,
            )
        return {"id": str(cid), **body.model_dump()}

    @r.put(f"{V}/versions/{{id}}/acceptance", tags=["release"], operation_id="setAcceptance")
    async def set_acceptance(id: uuid.UUID, template: dict[str, Any], actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            await gates.set_acceptance(conn, actor, id, template)
        return {"version_id": str(id), "template": template}

    @r.post(f"{V}/versions/{{id}}/submit", tags=["release"], operation_id="submitVersion")
    async def submit(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await gates.submit(conn, actor, id)

    @r.post(f"{V}/versions/{{id}}/run-tests", tags=["release"], operation_id="runTests")
    async def run_tests(id: uuid.UUID, request: Request, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await gates.run_lab(conn, actor, id, secrets=_secrets(request))

    @r.get(f"{V}/versions/{{id}}/gates", tags=["release"], operation_id="gateHistory")
    async def history(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return {"items": await gates.gate_history(conn, id)}

    @r.post(f"{V}/versions/{{id}}/canary", tags=["release"], operation_id="startCanary", status_code=201)
    async def canary(id: uuid.UUID, body: RolloutIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            rid = await gates.start_canary(
                conn, actor, id, env=body.env, canary_scope=body.scope, exposure=list(body.exposure)
            )
        return {"release_id": str(rid)}

    @r.post(f"{V}/versions/{{id}}/promote", tags=["release"], operation_id="promoteVersion")
    async def promote(id: uuid.UUID, body: RolloutIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await gates.promote(
                conn,
                actor,
                id,
                env=body.env,
                scope=body.scope,
                exposure=list(body.exposure),
                min_calls=body.min_calls,
                max_error_rate=body.max_error_rate,
            )

    @r.get(f"{V}/approvals", tags=["approval"], operation_id="listApprovals")
    async def pending(actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            rows = await approval.pending_for(conn, actor)
            items = []
            for row in rows:
                item = {k: (_str(v) if not isinstance(v, list) else [_str(x) for x in v]) for k, v in row.items()}
                item["subject"] = await _subject(conn, row)
                items.append(item)
        return {"items": items, "next_cursor": None, "has_more": False}

    @r.post(f"{V}/approvals/{{id}}/decide", tags=["approval"], operation_id="decideApproval")
    async def decide(id: uuid.UUID, body: DecisionIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            req = await approval.request_row(conn, id)
            if req["kind"] == "usage":
                status = await flows.decide_usage(
                    conn, actor, req["subject_id"], body.decision == "approve", body.comment
                )
            else:
                status = await gates.decide(
                    conn, actor, id, body.decision == "approve", body.comment, body.results, body.on_behalf_of
                )
        return {"id": str(id), "status": status}

    @r.post(f"{V}/approval-delegates", tags=["approval"], operation_id="addApprovalDelegate", status_code=201)
    async def add_delegate(body: DelegateIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            await approval.add_delegate(conn, actor, body.delegate_user_id, body.valid_to)
        return body.model_dump(mode="json")

    @r.post(f"{V}/usage-requests", tags=["grant"], operation_id="requestUsage", status_code=201)
    async def usage(body: UsageIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            rid = await flows.request_usage(
                conn, actor, consumer_id=body.consumer_id, asset_id=body.asset_id, env=body.env, reason=body.reason
            )
        return {"id": str(rid), "status": "pending", **body.model_dump(mode="json")}

    @r.post(f"{V}/access-reviews", tags=["grant"], operation_id="openAccessReview", status_code=201)
    async def open_review(body: ReviewIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            cid = await flows.open_review(
                conn, actor, body.company_id, expiring_days=body.expiring_days, unused_days=body.unused_days
            )
        return {"id": str(cid)}

    @r.post(f"{V}/access-reviews/{{id}}/close", tags=["grant"], operation_id="closeAccessReview")
    async def close_review(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return {"id": str(id), "revoked": await flows.close_review(conn, actor, id)}

    return r
