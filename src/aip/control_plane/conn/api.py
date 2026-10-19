"""连接与 Connector 实例接口（docs/api/control-plane.openapi.yaml）。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from aip.control_plane.conn import service as svc
from aip.control_plane.iam.api import ActorDep, DbDep

V = "/api/v1"


class ConnectionIn(BaseModel):
    company_id: uuid.UUID
    system_key: str
    env: Literal["sandbox", "test", "staging", "prod"]
    base_url: str
    scheme: Literal["https", "http"]
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
    kind: Literal["declarative", "kb", "mock"]
    system_connection_id: uuid.UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = Field(default=8, ge=1, le=256)
    rate_per_s: float = Field(default=20, gt=0)
    burst: int = Field(default=40, ge=1)


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

    @r.get(f"{V}/connections", operation_id="listConnections")
    async def list_connections(actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            rows = (await conn.execute(select(svc.system_connection).limit(200))).mappings().all()
        return {"items": [_out(dict(x)) for x in rows], "next_cursor": None, "has_more": False}

    @r.post(f"{V}/connections", operation_id="createConnection", status_code=201)
    async def create_connection(body: ConnectionIn, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        data = body.model_dump(mode="json", exclude_none=True, exclude={"company_id"})
        if body.transport_exception_expires_at is not None:
            data["transport_exception_expires_at"] = body.transport_exception_expires_at.isoformat()
        async with db.tx(actor.db_scope()) as conn:
            cid = await svc.create_connection(conn, actor, company_id=body.company_id, data=data)
            return _out(await svc.row(conn, cid))

    @r.post(f"{V}/connections/{{id}}/approve-exception", operation_id="approveTransportException")
    async def approve(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            status = await svc.approve_exception(conn, actor, id)
        return {"id": str(id), "status": status}

    @r.post(f"{V}/connections/{{id}}/test", operation_id="testConnection")
    async def test(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await svc.test_connection(conn, actor, id)

    @r.post(f"{V}/connections/{{id}}/trust-draft", operation_id="draftTrust")
    async def draft(id: uuid.UUID, actor: ActorDep, db: DbDep) -> dict[str, Any]:
        async with db.tx(actor.db_scope()) as conn:
            return await svc.draft_trust(conn, actor, id)

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

    return r
