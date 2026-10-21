"""控制面 Registry / Release / Grant / 发布快照接口（docs/api/control-plane.openapi.yaml）。"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.grant import service as grants
from aip.control_plane.iam.api import ActorDep, DbDep, _bearer
from aip.control_plane.registry import schemas as out
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import snapshot
from aip.control_plane.registry import tables as t
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.paging import Page
from aip.libs.tokens.keys import platform_key

V = "/api/v1"


class AssetIn(BaseModel):
    type: Literal["tool", "skill", "workflow", "bundle", "prompt", "model_profile", "connector_package"]
    name: str = Field(min_length=3, max_length=128)
    owner_org_id: uuid.UUID
    classification: int = Field(ge=1, le=4)


class VersionIn(BaseModel):
    semver: str
    spec: dict[str, Any]
    dep_lock: dict[str, Any] = Field(default_factory=dict)


class ReleaseIn(BaseModel):
    version_id: uuid.UUID
    env: Literal["test", "staging", "prod"]
    channel: Literal["beta", "stable"] = "stable"
    scope: dict[str, Any]
    exposure: list[Literal["rest", "mcp", "a2a"]] = ["rest"]


class CeilingIn(BaseModel):
    company_id: uuid.UUID
    asset_set: dict[str, list[uuid.UUID]]
    max_classification: int = Field(ge=1, le=4)
    quota: dict[str, int] = Field(default_factory=dict)


class GrantIn(BaseModel):
    asset_id: uuid.UUID
    grantee_type: Literal["consumer", "agent_profile", "org_unit", "user"]
    grantee_id: uuid.UUID
    env: Literal["test", "staging", "prod"]
    expires_at: dt.datetime
    ceiling_id: uuid.UUID


def _out(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID | dt.datetime) else v) for k, v in row.items()}


def router() -> APIRouter:
    r = APIRouter()

    @r.get(f"{V}/assets", tags=["registry"], operation_id="listAssets", response_model=Page[out.Asset])
    async def list_assets(actor: ActorDep, db: DbDep) -> Page[out.Asset]:
        from aip.control_plane.registry.list_enrich import enrich_asset_rows

        async with db.tx(actor.db_scope()) as conn:
            rows = (await conn.execute(select(t.asset).order_by(t.asset.c.name).limit(200))).mappings().all()
            enriched = await enrich_asset_rows(conn, [dict(x) for x in rows])
        return Page(items=[out.Asset.model_validate(_out(x)) for x in enriched])

    @r.post(
        f"{V}/assets",
        tags=["registry"],
        operation_id="createAsset",
        status_code=201,
        response_model=out.Asset,
    )
    async def create_asset(body: AssetIn, actor: ActorDep, db: DbDep) -> out.Asset:
        async with db.tx(actor.db_scope()) as conn:
            aid = await registry.create_asset(
                conn,
                actor,
                type_=body.type,
                name=body.name,
                owner_org_id=body.owner_org_id,
                classification=body.classification,
            )
            return out.Asset.model_validate(_out(await registry.asset_row(conn, aid)))

    @r.post(
        f"{V}/assets/{{id}}/versions",
        tags=["registry"],
        operation_id="createVersion",
        status_code=201,
        response_model=out.AssetVersion,
    )
    async def create_version(id: uuid.UUID, body: VersionIn, actor: ActorDep, db: DbDep) -> out.AssetVersion:
        async with db.tx(actor.db_scope()) as conn:
            vid = await registry.create_version(conn, actor, id, body.semver, body.spec, body.dep_lock)
            return out.AssetVersion.model_validate(_out(await registry.version_row(conn, vid)))

    @r.post(f"{V}/releases", tags=["release"], operation_id="createRelease", status_code=201)
    async def create_release(body: ReleaseIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            rid = await registry.create_release(
                conn,
                actor,
                body.version_id,
                env=body.env,
                channel=body.channel,
                scope=body.scope,
                exposure=list(body.exposure),
            )
            row = (await conn.execute(select(t.release).where(t.release.c.id == rid))).mappings().one()
        return _out(dict(row))

    @r.post(f"{V}/releases/{{id}}/rollback", tags=["release"], operation_id="rollbackRelease")
    async def rollback_release(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            restored = await registry.rollback(conn, actor, id)
            row = (await conn.execute(select(t.release).where(t.release.c.id == restored))).mappings().one()
        return _out(dict(row))

    @r.post(
        f"{V}/grant-ceilings",
        tags=["grant"],
        operation_id="createCeiling",
        status_code=201,
        response_model=out.GrantCeiling,
    )
    async def create_ceiling(body: CeilingIn, actor: ActorDep, db: DbDep) -> out.GrantCeiling:
        async with db.tx(actor.db_scope()) as conn:
            cid = await grants.create_ceiling(
                conn,
                actor,
                company_id=body.company_id,
                asset_ids=body.asset_set.get("asset_ids", []),
                max_classification=body.max_classification,
                quota=body.quota,
            )
        return out.GrantCeiling.model_validate({"id": str(cid), **body.model_dump(mode="json")})

    @r.put(f"{V}/grant-ceilings/{{id}}/quota", tags=["grant"], operation_id="setCeilingQuota")
    async def set_ceiling_quota(id: uuid.UUID, body: dict[str, Any], actor: ActorDep, db: DbDep) -> dict[str, Any]:
        """上限内全公司共享的每日调用额度：{"daily_calls": N}。"""
        async with db.tx(actor.db_scope()) as conn:
            return {"ceiling_id": str(id), "quota": await grants.set_ceiling_quota(conn, actor, id, body)}

    @r.get(f"{V}/grants", tags=["grant"], operation_id="listGrants", response_model=Page[out.Grant])
    async def list_grants(actor: ActorDep, db: DbDep) -> Page[out.Grant]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (await conn.execute(select(t.grant).limit(200))).mappings().all()
        return Page(items=[out.Grant.model_validate(_out(dict(x))) for x in rows])

    @r.post(
        f"{V}/grants",
        tags=["grant"],
        operation_id="createGrant",
        status_code=201,
        response_model=out.Grant,
    )
    async def create_grant(body: GrantIn, actor: ActorDep, db: DbDep) -> out.Grant:
        async with db.tx(actor.db_scope()) as conn:
            gid = await grants.create_grant(
                conn,
                actor,
                asset_id=body.asset_id,
                grantee_type=body.grantee_type,
                grantee_id=body.grantee_id,
                env=body.env,
                expires_at=body.expires_at,
                ceiling_id=body.ceiling_id,
            )
        return out.Grant.model_validate({"id": str(gid), **body.model_dump(mode="json")})

    @r.get(f"{V}/snapshots/release", tags=["snapshot"], operation_id="pullReleaseSnapshot", response_model=None)
    async def pull_release(
        request: Request,
        db: DbDep,
        after_seq: int = 0,
        wait_s: float = 0,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response | dict[str, Any]:
        if not secrets.compare_digest(_bearer(authorization), get_settings().internal_service_token):
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "需要服务凭据")

        async def fetch(conn: AsyncConnection) -> dict[str, Any] | None:
            await snapshot.publish(conn, platform_key())
            return await snapshot.latest(conn, after_seq)

        snap = await request.app.state.snapshot_feed.serve(fetch, wait_s)
        return snap if snap is not None else Response(status_code=204)

    return r
