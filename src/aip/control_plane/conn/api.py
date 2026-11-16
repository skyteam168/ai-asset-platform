"""连接与 Connector 实例接口（docs/api/control-plane.openapi.yaml）。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from aip.connectors.credentials import OpenBaoSecretStore, SecretStore
from aip.control_plane.conn import schemas as out
from aip.control_plane.conn import service as svc
from aip.control_plane.conn import sql_templates as sql_tpl
from aip.control_plane.iam.api import ActorDep, DbDep
from aip.libs.common.config import get_settings
from aip.libs.common.paging import Page

V = "/api/v1"


class ConnectionIn(BaseModel):
    company_id: uuid.UUID
    system_key: str
    env: Literal["sandbox", "test", "staging", "prod"]
    base_url: str
    scheme: Literal["https", "http", "postgres", "mysql"]
    tls_mode: Literal["system_ca", "custom_ca", "pinned", "insecure"] | None = None
    ca_pem: str | None = None
    pins: list[dict[str, str]] = Field(default_factory=list)
    tls_check_hostname: bool = True
    pin_enforce_expiry: bool = False
    block_on_fingerprint_change: bool | None = None
    credential_ref: str | None = None
    transport_exception_reason: str | None = None
    transport_exception_expires_at: dt.datetime | None = None


class ConnectorIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    kind: Literal["declarative", "kb", "mock", "upstream_mcp", "sql_readonly"]
    system_connection_id: uuid.UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = Field(default=8, ge=1, le=256)
    rate_per_s: float = Field(default=20, gt=0)
    burst: int = Field(default=40, ge=1)


class SqlTemplateIn(BaseModel):
    template_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    engine: Literal["postgres", "mysql"]
    statement: str = Field(min_length=8, max_length=20000)
    param_schema: dict[str, Any] = Field(default_factory=dict)
    max_rows: int = Field(default=200, ge=1, le=1000)
    statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    explain_cost_cap: float | None = None
    output_field_whitelist: list[str] = Field(default_factory=list)


class SqlTemplateTryIn(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


class UpstreamMcpSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    system_connection_id: uuid.UUID
    tools_manifest_hash: str = Field(min_length=64, max_length=64)
    mcp_path: str = Field(default="/mcp", min_length=1, max_length=256)
    pinned_tools: list[dict[str, Any]] = Field(default_factory=list)


class UpstreamMcpPreviewIn(BaseModel):
    system_connection_id: uuid.UUID
    mcp_path: str = Field(default="/mcp", min_length=1, max_length=256)


class CredentialsIn(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=512)
    api_key: str | None = Field(default=None, min_length=1, max_length=1024)
    token: str | None = Field(default=None, min_length=1, max_length=4096)
    credential_ref: str | None = Field(default=None, min_length=8, max_length=200)


class TestAuthIn(BaseModel):
    auth: dict[str, Any] = Field(default_factory=lambda: {"mode": "none"})


def _secrets(request: Request) -> SecretStore:
    s: SecretStore | None = getattr(request.app.state, "secrets", None)
    if s is None:
        settings = get_settings()
        s = OpenBaoSecretStore(settings.openbao_addr, settings.openbao_token)
        request.app.state.secrets = s
    return s


def _out(r: dict[str, Any]) -> dict[str, Any]:
    hidden = {"ca_pem"}
    return {
        k: (
            str(v)
            if isinstance(v, uuid.UUID | dt.datetime)
            else [str(x) for x in v]
            if isinstance(v, list) and v and isinstance(v[0], uuid.UUID)
            else v
        )
        for k, v in r.items()
        if k not in hidden
    }


def router() -> APIRouter:
    r = APIRouter(tags=["connection"])

    @r.get(f"{V}/connections", operation_id="listConnections", response_model=Page[out.SystemConnection])
    async def list_connections(actor: ActorDep, db: DbDep) -> Page[out.SystemConnection]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (await conn.execute(select(svc.system_connection).limit(200))).mappings().all()
        return Page(items=[out.SystemConnection.model_validate(_out(dict(x))) for x in rows])

    @r.post(
        f"{V}/connections",
        operation_id="createConnection",
        status_code=201,
        response_model=out.SystemConnection,
    )
    async def create_connection(body: ConnectionIn, actor: ActorDep, db: DbDep) -> out.SystemConnection:
        data = body.model_dump(mode="json", exclude_none=True, exclude={"company_id"})
        if body.transport_exception_expires_at is not None:
            data["transport_exception_expires_at"] = body.transport_exception_expires_at.isoformat()
        async with db.tx(actor.db_scope()) as conn:
            cid = await svc.create_connection(conn, actor, company_id=body.company_id, data=data)
            return out.SystemConnection.model_validate(_out(await svc.row(conn, cid)))

    @r.delete(f"{V}/connections/{{id}}", operation_id="deleteConnection", status_code=204)
    async def delete_connection(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Response:
        async with db.tx(actor.db_scope()) as conn:
            await svc.delete_connection(conn, actor, id)
        return Response(status_code=204)

    @r.post(
        f"{V}/connections/{{id}}/approve-exception",
        operation_id="approveTransportException",
        response_model=out.TransportExceptionStatus,
    )
    async def approve(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.TransportExceptionStatus:
        async with db.tx(actor.db_scope()) as conn:
            status = await svc.approve_exception(conn, actor, id)
        return out.TransportExceptionStatus(id=str(id), status=status)

    @r.post(
        f"{V}/connections/{{id}}/test",
        operation_id="testConnection",
        response_model=out.ConnectionTestResult,
    )
    async def test(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.ConnectionTestResult:
        async with db.tx(actor.db_scope()) as conn:
            return out.ConnectionTestResult.model_validate(await svc.test_connection(conn, actor, id))

    @r.put(
        f"{V}/connections/{{id}}/credentials",
        operation_id="putConnectionCredentials",
        response_model=out.CredentialRefOut,
    )
    async def put_credentials(
        id: uuid.UUID, body: CredentialsIn, actor: ActorDep, db: DbDep, request: Request
    ) -> out.CredentialRefOut:
        async with db.tx(actor.db_scope()) as conn:
            return out.CredentialRefOut.model_validate(
                await svc.put_credentials(
                    conn,
                    actor,
                    _secrets(request),
                    id,
                    username=body.username,
                    password=body.password,
                    api_key=body.api_key,
                    token=body.token,
                    credential_ref=body.credential_ref,
                )
            )

    @r.post(
        f"{V}/connections/{{id}}/test-auth",
        operation_id="testConnectionAuth",
        response_model=out.AuthTestResult,
    )
    async def test_auth(
        id: uuid.UUID, body: TestAuthIn, actor: ActorDep, db: DbDep, request: Request
    ) -> out.AuthTestResult:
        async with db.tx(actor.db_scope()) as conn:
            return out.AuthTestResult.model_validate(
                await svc.test_auth(conn, actor, _secrets(request), id, auth=body.auth)
            )

    @r.post(
        f"{V}/connections/{{id}}/trust-draft",
        operation_id="draftTrust",
        response_model=out.ConfigDraft,
    )
    async def draft(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.ConfigDraft:
        async with db.tx(actor.db_scope()) as conn:
            return out.ConfigDraft.model_validate(await svc.draft_trust(conn, actor, id))

    @r.get(
        f"{V}/connector-instances",
        operation_id="listConnectorInstances",
        response_model=Page[out.ConnectorInstance],
    )
    async def list_connectors(actor: ActorDep, db: DbDep) -> Page[out.ConnectorInstance]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (
                (await conn.execute(select(svc.connector_instance).order_by(svc.connector_instance.c.name).limit(200)))
                .mappings()
                .all()
            )
        return Page(items=[out.ConnectorInstance.model_validate(_out(dict(x))) for x in rows])

    @r.post(f"{V}/connector-instances", operation_id="createConnectorInstance", status_code=201)
    async def create_connector(body: ConnectorIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            iid = await svc.create_connector_instance(
                conn,
                actor,
                name=body.name,
                kind=body.kind,
                connection_id=body.system_connection_id,
                config=body.config,
                concurrency=body.concurrency,
                rate_per_s=body.rate_per_s,
                burst=body.burst,
            )
        return {"id": str(iid), **body.model_dump(mode="json")}

    @r.delete(f"{V}/connector-instances/{{id}}", operation_id="deleteConnectorInstance", status_code=204)
    async def delete_connector(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Response:
        async with db.tx(actor.db_scope()) as conn:
            await svc.delete_connector_instance(conn, actor, id)
        return Response(status_code=204)

    @r.post(
        f"{V}/connector-instances/{{id}}/sql-templates",
        operation_id="createSqlTemplate",
        status_code=201,
    )
    async def create_sql_template(id: uuid.UUID, body: SqlTemplateIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await sql_tpl.create_template(conn, actor, instance_id=id, body=body.model_dump())

    @r.get(f"{V}/connector-instances/{{id}}/sql-templates", operation_id="listSqlTemplates")
    async def list_sql_templates(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            items = await sql_tpl.list_templates(conn, actor, id)
        return {"items": items, "next_cursor": None, "has_more": False}

    @r.put(f"{V}/sql-templates/{{id}}", operation_id="updateSqlTemplate")
    async def update_sql_template(id: uuid.UUID, body: SqlTemplateIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await sql_tpl.update_template(conn, actor, id, body.model_dump())

    @r.post(f"{V}/sql-templates/{{id}}/try", operation_id="trySqlTemplate")
    async def try_sql_template(
        id: uuid.UUID, body: SqlTemplateTryIn, actor: ActorDep, db: DbDep, request: Request
    ) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await sql_tpl.try_template(conn, actor, _secrets(request), id, body.params)

    @r.post(f"{V}/sql-templates/{{id}}/activate", operation_id="activateSqlTemplate")
    async def activate_sql_template(id: uuid.UUID, actor: ActorDep, db: DbDep, request: Request) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await sql_tpl.activate_template(conn, actor, _secrets(request), id)

    @r.get(
        f"{V}/upstream-mcp-sources",
        operation_id="listUpstreamMcpSources",
        response_model=Page[out.UpstreamMcpSource],
    )
    async def list_upstream_sources(actor: ActorDep, db: DbDep) -> Page[out.UpstreamMcpSource]:
        async with db.tx(actor.db_scope()) as conn:
            rows = await svc.list_upstream_mcp_sources(conn, actor)
        return Page(items=[out.UpstreamMcpSource.model_validate(_out(x)) for x in rows])

    @r.post(
        f"{V}/upstream-mcp-sources/preview-hash",
        operation_id="previewUpstreamMcpHash",
        response_model=out.UpstreamMcpPreviewHash,
    )
    async def preview_upstream_hash(
        body: UpstreamMcpPreviewIn, actor: ActorDep, db: DbDep
    ) -> out.UpstreamMcpPreviewHash:
        async with db.tx(actor.db_scope()) as conn:
            result = await svc.preview_upstream_mcp_hash(
                conn, actor, connection_id=body.system_connection_id, mcp_path=body.mcp_path
            )
        return out.UpstreamMcpPreviewHash.model_validate(result)

    @r.post(
        f"{V}/upstream-mcp-sources",
        operation_id="createUpstreamMcpSource",
        status_code=201,
        response_model=out.UpstreamMcpSource,
    )
    async def create_upstream_source(body: UpstreamMcpSourceIn, actor: ActorDep, db: DbDep) -> out.UpstreamMcpSource:
        async with db.tx(actor.db_scope()) as conn:
            sid = await svc.create_upstream_mcp_source(
                conn,
                actor,
                name=body.name,
                connection_id=body.system_connection_id,
                tools_manifest_hash=body.tools_manifest_hash,
                mcp_path=body.mcp_path,
                pinned_tools=body.pinned_tools,
            )
            rows = await svc.list_upstream_mcp_sources(conn, actor)
        hit = next(x for x in rows if str(x["id"]) == str(sid))
        return out.UpstreamMcpSource.model_validate(_out(hit))

    @r.post(
        f"{V}/upstream-mcp-sources/{{id}}/disable",
        operation_id="disableUpstreamMcpSource",
        status_code=204,
    )
    async def disable_upstream_source(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Response:
        async with db.tx(actor.db_scope()) as conn:
            await svc.disable_upstream_mcp_source(conn, actor, id)
        return Response(status_code=204)

    @r.delete(f"{V}/upstream-mcp-sources/{{id}}", operation_id="deleteUpstreamMcpSource", status_code=204)
    async def delete_upstream_source(id: uuid.UUID, actor: ActorDep, db: DbDep) -> Response:
        async with db.tx(actor.db_scope()) as conn:
            await svc.delete_upstream_mcp_source(conn, actor, id)
        return Response(status_code=204)

    return r
