"""Importer HTTP：OpenAPI → 声明式 Connector / Tool 草稿。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.importer import service as importer

V = "/api/v1"


class OpenApiImportIn(BaseModel):
    document: str = Field(min_length=1, max_length=2_000_000)
    connector_name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    domain: str = Field(default="imported", pattern=r"^[a-z][a-z0-9_]*$")
    system: str = Field(default="imported", pattern=r"^[a-z][a-z0-9_]*$")
    persist: bool = False
    connection_id: uuid.UUID | None = None
    owner_org_id: uuid.UUID | None = None


def router() -> APIRouter:
    r = APIRouter(tags=["importer"])

    @r.post(f"{V}/importer/openapi", operation_id="importOpenApi", status_code=200)
    async def import_openapi(body: OpenApiImportIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await importer.import_openapi(
                conn,
                actor,
                document=body.document,
                connector_name=body.connector_name,
                domain=body.domain,
                system=body.system,
                persist=body.persist,
                connection_id=body.connection_id,
                owner_org_id=body.owner_org_id,
            )

    return r
