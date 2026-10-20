"""使用申请与复核活动（architecture.md 1 “使用要申请”、4.1 到期复核）。"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Column, DateTime, MetaData, Table, Text, and_, select, text, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.approval import service as approval
from aip.control_plane.grant.service import _grantee_company, create_grant, revoke_grant
from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.events import emit
from aip.control_plane.registry import tables as t
from aip.control_plane.registry.service import asset_row
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow

md = MetaData(schema="grants")
U = UUID(as_uuid=True)
usage_request = Table(
    "usage_request",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U),
    Column("consumer_id", U),
    Column("asset_id", U),
    Column("env", Text),
    Column("reason", Text),
    Column("requested_by", U),
    Column("approval_id", U),
    Column("grant_id", U),
    Column("status", Text),
)
review_campaign = Table(
    "access_review_campaign",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U),
    Column("due_at", DateTime(timezone=True)),
    Column("status", Text),
    Column("created_by", U),
)
review_item = Table(
    "access_review_item",
    md,
    Column("campaign_id", U, primary_key=True),
    Column("grant_id", U, primary_key=True),
    Column("company_id", U),
    Column("reason", Text),
    Column("decision", Text),
    Column("decided_by", U),
)
DEFAULT_GRANT_DAYS = 365
REVIEW_EXTENSION_DAYS = 90


async def ceiling_for(conn: AsyncConnection, company_id: uuid.UUID, asset_id: uuid.UUID) -> uuid.UUID | None:
    a = await asset_row(conn, asset_id)
    for c in (
        await conn.execute(
            select(t.grant_ceiling).where(
                and_(t.grant_ceiling.c.company_id == company_id, t.grant_ceiling.c.status == "active")
            )
        )
    ).mappings():
        if asset_id in c["asset_ids"] and a["classification"] <= c["max_classification"]:
            return uuid.UUID(str(c["id"]))
    return None


async def request_usage(
    conn: AsyncConnection, actor: Actor, *, consumer_id: uuid.UUID, asset_id: uuid.UUID, env: str, reason: str
) -> uuid.UUID:
    """只能在本公司授权上限内申请；超出上限须先由总部扩大上限。审批人为本公司管理员（不能是申请人）。"""
    company = await _grantee_company(conn, "consumer", consumer_id)
    actor.require(
        actor.has("platform_admin") or actor.manages_org(company) or actor.company_id == company,
        "只能为本公司的消费方申请",
    )
    if await ceiling_for(conn, company, asset_id) is None:
        raise PlatformError(ErrorCode.PERMISSION_DENIED, "该资产不在本公司授权上限内，需总部先扩大上限")
    rid = new_id()
    aid = await approval.create(
        conn,
        kind="usage",
        subject_id=rid,
        company_id=company,
        submitter=actor.user_id,
        approver_role="company_admin",
        scope_org_id=company,
        sla_business_days=2,
        payload={"asset_id": str(asset_id), "consumer_id": str(consumer_id), "env": env},
    )
    await conn.execute(
        usage_request.insert().values(
            id=rid,
            company_id=company,
            consumer_id=consumer_id,
            asset_id=asset_id,
            env=env,
            reason=reason,
            requested_by=actor.user_id,
            approval_id=aid,
            status="pending",
        )
    )
    return rid


async def decide_usage(
    conn: AsyncConnection, actor: Actor, request_id: uuid.UUID, approve: bool, comment: str = ""
) -> str:
    req = (await conn.execute(select(usage_request).where(usage_request.c.id == request_id))).mappings().first()
    if req is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "使用申请不存在")
    status = await approval.decide(conn, actor, req["approval_id"], approve, comment)
    if status == "approved":
        ceiling = await ceiling_for(conn, req["company_id"], req["asset_id"])
        if ceiling is None:
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "授权上限已变化，无法批准")
        gid = await create_grant(
            conn,
            actor,
            asset_id=req["asset_id"],
            grantee_type="consumer",
            grantee_id=req["consumer_id"],
            env=req["env"],
            expires_at=utcnow() + dt.timedelta(days=DEFAULT_GRANT_DAYS),
            ceiling_id=ceiling,
        )
        await conn.execute(
            update(usage_request).where(usage_request.c.id == request_id).values(status="approved", grant_id=gid)
        )
    elif status == "rejected":
        await conn.execute(update(usage_request).where(usage_request.c.id == request_id).values(status="rejected"))
    return status


async def open_review(
    conn: AsyncConnection,
    actor: Actor,
    company_id: uuid.UUID,
    *,
    expiring_days: int = 30,
    unused_days: int = 90,
    due_days: int = 14,
) -> uuid.UUID:
    """列出即将到期与长期未使用的授权；截止前未确认的在关闭时自动失效。"""
    actor.require(actor.has("platform_admin") or actor.manages_org(company_id))
    now = utcnow()
    cid = new_id()
    await conn.execute(
        review_campaign.insert().values(
            id=cid,
            company_id=company_id,
            status="open",
            due_at=now + dt.timedelta(days=due_days),
            created_by=actor.user_id,
        )
    )
    rows = (
        await conn.execute(
            text(
                "SELECT g.id, g.expires_at, (SELECT max(e.created_at) FROM exec.tool_execution e "
                "  JOIN registry.asset_version v ON v.spec->>'capability_id' = e.capability_id "
                "  WHERE v.asset_id = g.asset_id AND e.consumer_id = g.grantee_id AND e.status = 'succeeded') "
                "AS last_used "
                "FROM grants.\"grant\" g WHERE g.company_id = :c AND g.status = 'active'"
            ),
            {"c": company_id},
        )
    ).mappings()
    for g in rows:
        reasons = []
        if g["expires_at"] <= now + dt.timedelta(days=expiring_days):
            reasons.append("即将到期")
        if g["last_used"] is None or g["last_used"] <= now - dt.timedelta(days=unused_days):
            reasons.append(f"{unused_days} 天未使用")
        if reasons:
            await conn.execute(
                review_item.insert().values(
                    campaign_id=cid, grant_id=g["id"], company_id=company_id, reason="、".join(reasons)
                )
            )
    return cid


async def review_decide(
    conn: AsyncConnection, actor: Actor, campaign_id: uuid.UUID, grant_id: uuid.UUID, keep: bool
) -> None:
    item = (
        (
            await conn.execute(
                select(review_item).where(
                    and_(review_item.c.campaign_id == campaign_id, review_item.c.grant_id == grant_id)
                )
            )
        )
        .mappings()
        .first()
    )
    if item is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "复核项不存在")
    actor.require(actor.has("platform_admin") or actor.manages_org(item["company_id"]))
    await conn.execute(
        update(review_item)
        .where(and_(review_item.c.campaign_id == campaign_id, review_item.c.grant_id == grant_id))
        .values(decision="keep" if keep else "revoke", decided_by=actor.user_id)
    )
    if keep:
        await conn.execute(
            update(t.grant)
            .where(t.grant.c.id == grant_id)
            .values(expires_at=utcnow() + dt.timedelta(days=REVIEW_EXTENSION_DAYS))
        )
        await emit(conn, "grant.renewed", item["company_id"], grant_id, actor.user_id)
    else:
        await revoke_grant(conn, actor, grant_id)


async def close_review(conn: AsyncConnection, actor: Actor, campaign_id: uuid.UUID) -> int:
    """关闭复核活动：未处理的授权一律撤销，返回撤销数量。"""
    camp = (await conn.execute(select(review_campaign).where(review_campaign.c.id == campaign_id))).mappings().first()
    if camp is None or camp["status"] != "open":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "复核活动不存在或已关闭")
    actor.require(actor.has("platform_admin") or actor.manages_org(camp["company_id"]))
    pending = [
        r[0]
        for r in await conn.execute(
            select(review_item.c.grant_id).where(
                and_(review_item.c.campaign_id == campaign_id, review_item.c.decision.is_(None))
            )
        )
    ]
    for gid in pending:
        await revoke_grant(conn, Actor.system_actor(), gid)
        await conn.execute(
            update(review_item)
            .where(and_(review_item.c.campaign_id == campaign_id, review_item.c.grant_id == gid))
            .values(decision="revoke")
        )
    await conn.execute(update(review_campaign).where(review_campaign.c.id == campaign_id).values(status="closed"))
    return len(pending)
