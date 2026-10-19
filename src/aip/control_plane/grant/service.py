"""授权上限与授权下放（architecture.md 4.1、4.2）。完整的使用申请、审批与复核活动在 P7（M4）。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.events import emit
from aip.control_plane.iam.orgs import get_org
from aip.control_plane.registry import tables as t
from aip.control_plane.registry.service import asset_row
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow

GRANTEE_TYPES = frozenset({"consumer", "agent_profile", "org_unit", "user"})


async def create_ceiling(
    conn: AsyncConnection,
    actor: Actor,
    *,
    company_id: uuid.UUID,
    asset_ids: list[uuid.UUID],
    max_classification: int,
    domains: list[str] | None = None,
) -> uuid.UUID:
    actor.require(actor.has("platform_admin"), "授权上限由总部设定")
    org = await get_org(conn, company_id)
    if org.type != "company":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "授权上限按公司设定")
    cid = new_id()
    await conn.execute(
        t.grant_ceiling.insert().values(
            id=cid,
            company_id=company_id,
            asset_ids=asset_ids,
            domains=domains or [],
            max_classification=max_classification,
            quota={},
            status="active",
            created_by=actor.user_id,
        )
    )
    await emit(conn, "ceiling.created", company_id, cid, actor.user_id)
    return cid


async def revoke_ceiling(conn: AsyncConnection, actor: Actor, ceiling_id: uuid.UUID) -> None:
    """撤销上限后，引用它的授权在下一份快照中自动失效（不做级联删除）。"""
    actor.require(actor.has("platform_admin"))
    row = (await conn.execute(select(t.grant_ceiling.c.company_id).where(t.grant_ceiling.c.id == ceiling_id))).first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "授权上限不存在")
    await conn.execute(update(t.grant_ceiling).where(t.grant_ceiling.c.id == ceiling_id).values(status="revoked"))
    await emit(conn, "ceiling.revoked", row[0], ceiling_id, actor.user_id)


async def _grantee_company(conn: AsyncConnection, grantee_type: str, grantee_id: uuid.UUID) -> uuid.UUID:
    from aip.control_plane.iam import tables as iam

    table, col = {
        "consumer": (iam.consumer, iam.consumer.c.id),
        "agent_profile": (iam.agent_profile, iam.agent_profile.c.id),
        "org_unit": (iam.org_unit, iam.org_unit.c.id),
        "user": (iam.iam_user, iam.iam_user.c.id),
    }[grantee_type]
    row = (await conn.execute(select(table.c.company_id).where(col == grantee_id))).first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "授权对象不存在或不可见")
    return uuid.UUID(str(row[0]))


async def create_grant(
    conn: AsyncConnection,
    actor: Actor,
    *,
    asset_id: uuid.UUID,
    grantee_type: str,
    grantee_id: uuid.UUID,
    env: str,
    expires_at: dt.datetime,
    ceiling_id: uuid.UUID,
) -> uuid.UUID:
    """授权必须落在所属公司的授权上限内；分公司只能授予本公司对象，不能转授给其他公司。"""
    if grantee_type not in GRANTEE_TYPES:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "未知授权对象类型")
    company = await _grantee_company(conn, grantee_type, grantee_id)
    actor.require(actor.has("platform_admin") or actor.manages_org(company), "只能授予本公司的对象")
    ceiling = (await conn.execute(select(t.grant_ceiling).where(t.grant_ceiling.c.id == ceiling_id))).mappings().first()
    a = await asset_row(conn, asset_id)
    if ceiling is None or ceiling["status"] != "active" or ceiling["company_id"] != company:
        raise PlatformError(ErrorCode.PERMISSION_DENIED, "授权上限不存在、已撤销或不属于该公司")
    if asset_id not in ceiling["asset_ids"] or a["classification"] > ceiling["max_classification"]:
        raise PlatformError(ErrorCode.PERMISSION_DENIED, "超出授权上限")
    if expires_at <= utcnow():
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "授权必须带未来的到期日")
    gid = new_id()
    await conn.execute(
        t.grant.insert().values(
            id=gid,
            asset_id=asset_id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            env=env,
            company_id=company,
            ceiling_id=ceiling_id,
            grantor=actor.user_id,
            expires_at=expires_at,
            status="active",
        )
    )
    await emit(conn, "grant.created", company, gid, actor.user_id, {"asset_id": str(asset_id)})
    return gid


async def revoke_grant(conn: AsyncConnection, actor: Actor, grant_id: uuid.UUID) -> None:
    row = (await conn.execute(select(t.grant.c.company_id).where(t.grant.c.id == grant_id))).first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "授权不存在")
    actor.require(actor.has("platform_admin") or actor.manages_org(row[0]))
    await conn.execute(update(t.grant).where(t.grant.c.id == grant_id).values(status="revoked"))
    await emit(conn, "grant.revoked", row[0], grant_id, actor.user_id)


async def snapshot_part(conn: AsyncConnection) -> dict[str, Any]:
    """供 IAM 快照使用：只导出有效且仍在有效上限内的授权（上限撤销 → 授权自动失效）。"""
    now = utcnow()
    ceilings = {
        r["id"]: r
        for r in (await conn.execute(select(t.grant_ceiling).where(t.grant_ceiling.c.status == "active"))).mappings()
    }
    classification = {
        r.id: r.classification for r in (await conn.execute(select(t.asset.c.id, t.asset.c.classification))).all()
    }
    grants = []
    for g in (
        await conn.execute(select(t.grant).where(and_(t.grant.c.status == "active", t.grant.c.expires_at > now)))
    ).mappings():
        c = ceilings.get(g["ceiling_id"])
        within = (
            c is not None
            and g["asset_id"] in c["asset_ids"]
            and classification.get(g["asset_id"], 5) <= c["max_classification"]
        )
        grants.append(
            {
                "id": str(g["id"]),
                "asset_id": str(g["asset_id"]),
                "grantee_type": g["grantee_type"],
                "grantee_id": str(g["grantee_id"]),
                "env": g["env"],
                "company_id": str(g["company_id"]),
                "within_ceiling": within,
                "expires_at": g["expires_at"].isoformat(),
            }
        )
    return {"grants": sorted(grants, key=lambda x: x["id"])}
