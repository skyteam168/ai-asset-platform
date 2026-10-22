"""Importer HTTP：OpenAPI / Postman → 声明式 Connector / Tool 草稿；预览真连接试调。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from aip.connectors.credentials import OpenBaoSecretStore, SecretStore
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.importer import schemas as out
from aip.control_plane.importer import service as importer
from aip.control_plane.importer import try_op
from aip.libs.common.config import get_settings

V = "/api/v1"


class ImportIn(BaseModel):
    document: str = Field(min_length=1, max_length=2_000_000)
    connector_name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    domain: str = Field(default="imported", pattern=r"^[a-z][a-z0-9_]*$")
    system: str = Field(default="imported", pattern=r"^[a-z][a-z0-9_]*$")
    persist: bool = False
    connection_id: uuid.UUID | None = None
    owner_org_id: uuid.UUID | None = None
    enrich_descriptions: bool = False
    model_profile: str | None = Field(
        default=None, description="enrich_descriptions=true 时必填，格式 name@semver"
    )


class TryOperationIn(BaseModel):
    connection_id: uuid.UUID
    connector_config: dict[str, Any] = Field(description="含 operations 的声明式配置（通常来自预览结果）")
    operation: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    allow_write: bool = Field(
        default=False,
        description="写操作须显式 true；仅 sandbox/test 环境连接",
    )


def _secrets(request: Request) -> SecretStore:
    s: SecretStore | None = getattr(request.app.state, "secrets", None)
    if s is None:
        settings = get_settings()
        s = OpenBaoSecretStore(settings.openbao_addr, settings.openbao_token)
        request.app.state.secrets = s
    return s


def router() -> APIRouter:
    r = APIRouter(tags=["importer"])

    @r.post(
        f"{V}/importer/openapi",
        operation_id="importOpenApi",
        status_code=200,
        response_model=out.ImporterResult,
    )
    async def import_openapi(body: ImportIn, actor: ActorDep, db: DbDep) -> out.ImporterResult:
        async with db.tx(actor.db_scope()) as conn:
            return out.ImporterResult.model_validate(
                await importer.import_openapi(
                    conn,
                    actor,
                    document=body.document,
                    connector_name=body.connector_name,
                    domain=body.domain,
                    system=body.system,
                    persist=body.persist,
                    connection_id=body.connection_id,
                    owner_org_id=body.owner_org_id,
                    enrich_descriptions=body.enrich_descriptions,
                    model_profile=body.model_profile,
                )
            )

    @r.post(
        f"{V}/importer/postman",
        operation_id="importPostman",
        status_code=200,
        response_model=out.ImporterResult,
    )
    async def import_postman(body: ImportIn, actor: ActorDep, db: DbDep) -> out.ImporterResult:
        async with db.tx(actor.db_scope()) as conn:
            return out.ImporterResult.model_validate(
                await importer.import_postman(
                    conn,
                    actor,
                    document=body.document,
                    connector_name=body.connector_name,
                    domain=body.domain,
                    system=body.system,
                    persist=body.persist,
                    connection_id=body.connection_id,
                    owner_org_id=body.owner_org_id,
                    enrich_descriptions=body.enrich_descriptions,
                    model_profile=body.model_profile,
                )
            )

    @r.post(
        f"{V}/importer/try-operation",
        operation_id="tryImporterOperation",
        status_code=200,
        response_model=out.ImporterTryResult,
    )
    async def try_importer_operation(
        body: TryOperationIn, actor: ActorDep, db: DbDep, request: Request
    ) -> out.ImporterTryResult:
        async with db.tx(actor.db_scope()) as conn:
            return out.ImporterTryResult.model_validate(
                await try_op.try_operation(
                    conn,
                    actor,
                    _secrets(request),
                    connection_id=body.connection_id,
                    connector_config=body.connector_config,
                    operation=body.operation,
                    params=body.params,
                    allow_write=body.allow_write,
                )
            )

    return r
