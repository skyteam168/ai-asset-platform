"""契约漂移运维 API（H46）。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.probe import service as probe
from aip.libs.common.paging import Page

V = "/api/v1"


class ContractDrift(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_id: uuid.UUID
    company_id: uuid.UUID
    capability_id: str
    marked_at: Any = None
    reason: str = ""
    consecutive_failures: int = 0
    last_g1_at: Any = None
    cleared_at: Any = None


class DriftCleared(BaseModel):
    version_id: uuid.UUID
    cleared_at: Any = None


def router() -> APIRouter:
    r = APIRouter()

    @r.get(
        f"{V}/contract-drifts",
        tags=["gates"],
        operation_id="listContractDrifts",
        response_model=Page[ContractDrift],
    )
    async def list_drifts(actor: ActorDep, db: DbDep) -> Page[ContractDrift]:
        actor.require(
            actor.has("platform_admin") or actor.has("asset_developer") or actor.has("data_owner"),
            "需要平台角色",
        )
        async with db.tx(actor.db_scope()) as conn:
            items = [ContractDrift.model_validate(x) for x in await probe.list_drifts(conn)]
        return Page(items=items)

    @r.post(
        f"{V}/contract-drifts/{{version_id}}/clear",
        tags=["gates"],
        operation_id="clearContractDrift",
        response_model=DriftCleared,
    )
    async def clear(version_id: uuid.UUID, actor: ActorDep, db: DbDep) -> DriftCleared:
        async with db.tx(actor.db_scope()) as conn:
            row = await probe.clear_drift(conn, actor, version_id)
        return DriftCleared(version_id=version_id, cleared_at=row.get("cleared_at"))

    return r
