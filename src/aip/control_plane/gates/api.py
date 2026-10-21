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
from aip.control_plane.gates import schemas as out
from aip.control_plane.gates import service as gates
from aip.control_plane.grant import flows
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.libs.common.config import get_settings
from aip.libs.common.paging import Page

V = "/api/v1"


class CaseIn(BaseModel):
    name: str
    kind: Literal["functional", "permission"]
    args: dict[str, Any]
    expect: dict[str, Any]
    principal: dict[str, Any] = Field(default_factory=dict)
    critical: bool = True


class SelectionEvalCaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    expect_capability_id: str | None = None


class SelectionEvalIn(BaseModel):
    model_profiles: list[str] = Field(min_length=1, max_length=2)
    cases: list[SelectionEvalCaseIn] = Field(default_factory=list)


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
    if row["kind"] == "resume":
        rule = row["payload"].get("rule_code", "")
        caps = {s.get("semver", "") for s in row["payload"].get("suspended", [])}
        return f"风险熔断恢复：{rule}（{len(caps)} 个版本）"
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

    @r.post(
        f"{V}/versions/{{id}}/test-cases",
        tags=["release"],
        operation_id="addTestCase",
        status_code=201,
        response_model=out.TestCase,
    )
    async def add_case(id: uuid.UUID, body: CaseIn, actor: ActorDep, db: DbDep) -> out.TestCase:
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
        return out.TestCase.model_validate({"id": str(cid), **body.model_dump()})

    @r.put(
        f"{V}/versions/{{id}}/selection-eval",
        tags=["release"],
        operation_id="putSelectionEval",
        response_model=out.SelectionEval,
    )
    async def put_selection_eval(id: uuid.UUID, body: SelectionEvalIn, actor: ActorDep, db: DbDep) -> out.SelectionEval:
        async with db.tx(actor.db_scope()) as conn:
            data = await gates.put_selection_eval(
                conn,
                actor,
                id,
                model_profiles=body.model_profiles,
                cases=[c.model_dump() for c in body.cases],
            )
        return out.SelectionEval.model_validate(data)

    @r.get(
        f"{V}/versions/{{id}}/selection-eval",
        tags=["release"],
        operation_id="getSelectionEval",
        response_model=out.SelectionEval,
    )
    async def get_selection_eval(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.SelectionEval:
        from aip.control_plane.registry import service as registry

        async with db.tx(actor.db_scope()) as conn:
            await registry.version_row(conn, id)
            return out.SelectionEval.model_validate(await gates.get_selection_eval(conn, id))

    @r.put(
        f"{V}/versions/{{id}}/acceptance",
        tags=["release"],
        operation_id="setAcceptance",
        response_model=out.AcceptanceSheet,
    )
    async def set_acceptance(
        id: uuid.UUID, template: dict[str, Any], actor: ActorDep, db: DbDep
    ) -> out.AcceptanceSheet:
        async with db.tx(actor.db_scope()) as conn:
            await gates.set_acceptance(conn, actor, id, template)
        return out.AcceptanceSheet(version_id=str(id), template=template)

    @r.post(
        f"{V}/versions/{{id}}/submit",
        tags=["release"],
        operation_id="submitVersion",
        response_model=out.GateSubmitResult,
    )
    async def submit(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.GateSubmitResult:
        async with db.tx(actor.db_scope()) as conn:
            return out.GateSubmitResult.model_validate(await gates.submit(conn, actor, id))

    @r.post(
        f"{V}/versions/{{id}}/run-tests",
        tags=["release"],
        operation_id="runTests",
        response_model=out.LabRunResult,
    )
    async def run_tests(id: uuid.UUID, request: Request, actor: ActorDep, db: DbDep) -> out.LabRunResult:
        llm = getattr(request.app.state, "selection_llm", None)
        async with db.tx(actor.db_scope()) as conn:
            return out.LabRunResult.model_validate(
                await gates.run_lab(conn, actor, id, secrets=_secrets(request), llm=llm)
            )

    @r.get(
        f"{V}/versions/{{id}}/gates",
        tags=["release"],
        operation_id="gateHistory",
        response_model=Page[out.GateResult],
    )
    async def history(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Page[out.GateResult]:
        async with db.tx(actor.db_scope()) as conn:
            items = [out.GateResult.model_validate(x) for x in await gates.gate_history(conn, id)]
        return Page(items=items)

    @r.get(
        f"{V}/versions/{{id}}/impact",
        tags=["release"],
        operation_id="listVersionImpact",
        response_model=out.VersionImpact,
    )
    async def impact(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.VersionImpact:
        """G4 影响面：因本版本变更需回归的上层资产（architecture.md 6.4）。"""
        from aip.control_plane.gates.impact import list_impact

        async with db.tx(actor.db_scope()) as conn:
            items = await list_impact(conn, id)
        return out.VersionImpact.model_validate({"version_id": str(id), "items": items})

    @r.post(
        f"{V}/versions/{{id}}/canary",
        tags=["release"],
        operation_id="startCanary",
        status_code=201,
        response_model=out.CanaryStarted,
    )
    async def canary(id: uuid.UUID, body: RolloutIn, actor: ActorDep, db: DbDep) -> out.CanaryStarted:
        async with db.tx(actor.db_scope()) as conn:
            rid = await gates.start_canary(
                conn, actor, id, env=body.env, canary_scope=body.scope, exposure=list(body.exposure)
            )
        return out.CanaryStarted(release_id=str(rid))

    @r.post(
        f"{V}/versions/{{id}}/promote",
        tags=["release"],
        operation_id="promoteVersion",
        response_model=out.PromoteResult,
    )
    async def promote(id: uuid.UUID, body: RolloutIn, actor: ActorDep, db: DbDep) -> out.PromoteResult:
        async with db.tx(actor.db_scope()) as conn:
            return out.PromoteResult.model_validate(
                await gates.promote(
                    conn,
                    actor,
                    id,
                    env=body.env,
                    scope=body.scope,
                    exposure=list(body.exposure),
                    min_calls=body.min_calls,
                    max_error_rate=body.max_error_rate,
                )
            )

    @r.get(
        f"{V}/approvals",
        tags=["approval"],
        operation_id="listApprovals",
        response_model=Page[out.ApprovalPending],
    )
    async def pending(actor: ActorDep, db: DbDep) -> Page[out.ApprovalPending]:
        async with db.tx(actor.db_scope()) as conn:
            rows = await approval.pending_for(conn, actor)
            items = []
            for row in rows:
                item = {k: (_str(v) if not isinstance(v, list) else [_str(x) for x in v]) for k, v in row.items()}
                item["subject"] = await _subject(conn, row)
                items.append(out.ApprovalPending.model_validate(item))
        return Page(items=items)

    @r.post(
        f"{V}/approvals/{{id}}/decide",
        tags=["approval"],
        operation_id="decideApproval",
        response_model=out.ApprovalDecision,
    )
    async def decide(id: uuid.UUID, body: DecisionIn, actor: ActorDep, db: DbDep) -> out.ApprovalDecision:
        async with db.tx(actor.db_scope()) as conn:
            req = await approval.request_row(conn, id)
            if req["kind"] == "resume":
                from aip.control_plane.risk import service as risk

                status = await risk.decide_resume(
                    conn, actor, id, body.decision == "approve", body.comment, body.on_behalf_of
                )
            elif req["kind"] == "usage":
                status = await flows.decide_usage(
                    conn, actor, req["subject_id"], body.decision == "approve", body.comment
                )
            else:
                status = await gates.decide(
                    conn, actor, id, body.decision == "approve", body.comment, body.results, body.on_behalf_of
                )
        return out.ApprovalDecision(id=str(id), status=status)

    @r.post(
        f"{V}/approval-delegates",
        tags=["approval"],
        operation_id="addApprovalDelegate",
        status_code=201,
        response_model=out.ApprovalDelegate,
    )
    async def add_delegate(body: DelegateIn, actor: ActorDep, db: DbDep) -> out.ApprovalDelegate:
        async with db.tx(actor.db_scope()) as conn:
            await approval.add_delegate(conn, actor, body.delegate_user_id, body.valid_to)
        return out.ApprovalDelegate.model_validate(body.model_dump(mode="json"))

    @r.post(
        f"{V}/usage-requests",
        tags=["grant"],
        operation_id="requestUsage",
        status_code=201,
        response_model=out.UsageRequestCreated,
    )
    async def usage(body: UsageIn, actor: ActorDep, db: DbDep) -> out.UsageRequestCreated:
        async with db.tx(actor.db_scope()) as conn:
            rid = await flows.request_usage(
                conn, actor, consumer_id=body.consumer_id, asset_id=body.asset_id, env=body.env, reason=body.reason
            )
        return out.UsageRequestCreated.model_validate(
            {"id": str(rid), "status": "pending", **body.model_dump(mode="json")}
        )

    @r.post(
        f"{V}/access-reviews",
        tags=["grant"],
        operation_id="openAccessReview",
        status_code=201,
        response_model=out.AccessReviewOpened,
    )
    async def open_review(body: ReviewIn, actor: ActorDep, db: DbDep) -> out.AccessReviewOpened:
        async with db.tx(actor.db_scope()) as conn:
            cid = await flows.open_review(
                conn, actor, body.company_id, expiring_days=body.expiring_days, unused_days=body.unused_days
            )
        return out.AccessReviewOpened(id=str(cid))

    @r.post(
        f"{V}/access-reviews/{{id}}/close",
        tags=["grant"],
        operation_id="closeAccessReview",
        response_model=out.AccessReviewClosed,
    )
    async def close_review(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.AccessReviewClosed:
        async with db.tx(actor.db_scope()) as conn:
            revoked = await flows.close_review(conn, actor, id)
        return out.AccessReviewClosed(id=str(id), revoked=revoked)

    return r
