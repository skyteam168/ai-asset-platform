"""系统连接、传输例外审批、连接测试、Connector 实例（architecture.md 9.2、9.6、9.7）。"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import jsonschema
from sqlalchemy import (
    ARRAY,
    REAL,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import CIDR, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.orgs import get_org
from aip.control_plane.registry.service import SPECS, notify_change
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.httpclient import ConnectionConfig, NetworkAllowlist, probe, trust_draft
from aip.libs.httpclient.config import Resolver

md = MetaData(schema="conn")
U = UUID(as_uuid=True)
system_connection = Table(
    "system_connection",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U),
    Column("system_key", Text),
    Column("env", Text),
    Column("base_url", Text),
    Column("scheme", Text),
    Column("tls_mode", Text),
    Column("ca_pem", Text),
    Column("pins", JSONB),
    Column("tls_check_hostname", Boolean),
    Column("pin_enforce_expiry", Boolean),
    Column("block_on_fingerprint_change", Boolean),
    Column("client_cert_ref", Text),
    Column("credential_ref", Text),
    Column("connect_timeout_s", REAL),
    Column("read_timeout_s", REAL),
    Column("transport_exception_reason", Text),
    Column("transport_exception_expires_at", DateTime(timezone=True)),
    Column("transport_approved_by", ARRAY(U)),
    Column("tls_last_seen_fingerprint", Text),
    Column("owner_user_id", U),
    Column("status", Text),
    Column("updated_at", DateTime(timezone=True)),
)
network_allowlist = Table("network_allowlist", md, Column("cidr", CIDR, primary_key=True), Column("description", Text))
connection_test_run = Table(
    "connection_test_run",
    md,
    Column("id", U, primary_key=True),
    Column("connection_id", U),
    Column("company_id", U),
    Column("run_at", DateTime(timezone=True)),
    Column("verdict", Text),
    Column("error_code", Text),
    Column("result", JSONB),
    Column("operator", U),
)
connector_instance = Table(
    "connector_instance",
    md,
    Column("id", U, primary_key=True),
    Column("name", Text),
    Column("company_id", U),
    Column("kind", Text),
    Column("system_connection_id", U),
    Column("config", JSONB),
    Column("concurrency", Integer),
    Column("rate_per_s", REAL),
    Column("burst", Integer),
    Column("status", Text),
)

MAX_EXCEPTION_DAYS = 90
_SCHEMA = jsonschema.Draft202012Validator(
    json.loads((SPECS / "system-connection.schema.json").read_text(encoding="utf-8"))
)


def _manages(actor: Actor) -> bool:
    return actor.has("platform_admin") or actor.has("asset_developer")


async def row(conn: AsyncConnection, connection_id: uuid.UUID) -> dict[str, Any]:
    r = (
        (await conn.execute(select(system_connection).where(system_connection.c.id == connection_id)))
        .mappings()
        .first()
    )
    if r is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "连接不存在或不可见")
    return dict(r)


def to_config(r: dict[str, Any]) -> ConnectionConfig:
    return ConnectionConfig.from_dict({**r, "id": str(r["id"])})


async def allowlist(conn: AsyncConnection) -> NetworkAllowlist:
    return NetworkAllowlist.parse([str(c) for (c,) in (await conn.execute(select(network_allowlist.c.cidr))).all()])


async def create_connection(
    conn: AsyncConnection, actor: Actor, *, company_id: uuid.UUID, data: dict[str, Any]
) -> uuid.UUID:
    """传输例外（http、insecure）创建后处于 pending_approval，审批通过才 active。"""
    actor.require(_manages(actor), "连接由总部 IT 维护")
    errors = list(_SCHEMA.iter_errors(data))
    if errors:
        raise PlatformError(
            ErrorCode.INVALID_ARGUMENT,
            "连接配置不合法",
            [
                {"field": ".".join(str(p) for p in e.path) or "$", "code": "INVALID_ARGUMENT", "message": e.message}
                for e in errors[:10]
            ],
        )
    org = await get_org(conn, company_id)
    if org.type != "company":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "连接按公司登记")
    exception = data["scheme"] == "http" or data.get("tls_mode") == "insecure"
    expires = data.get("transport_exception_expires_at")
    if exception:
        expires_at = dt.datetime.fromisoformat(expires) if isinstance(expires, str) else expires
        if expires_at is None or expires_at > utcnow() + dt.timedelta(days=MAX_EXCEPTION_DAYS):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"传输例外到期日不能超过 {MAX_EXCEPTION_DAYS} 天")
        data = {**data, "transport_exception_expires_at": expires_at}
    cid = new_id()
    await conn.execute(
        system_connection.insert().values(
            id=cid,
            company_id=company_id,
            system_key=data["system_key"],
            env=data["env"],
            base_url=data["base_url"],
            scheme=data["scheme"],
            tls_mode=data.get("tls_mode"),
            ca_pem=data.get("ca_pem"),
            pins=data.get("pins", []),
            tls_check_hostname=data.get("tls_check_hostname", True),
            pin_enforce_expiry=data.get("pin_enforce_expiry", False),
            block_on_fingerprint_change=data.get("block_on_fingerprint_change", data.get("env") == "prod"),
            client_cert_ref=data.get("client_cert_ref"),
            credential_ref=data.get("credential_ref"),
            transport_exception_reason=data.get("transport_exception_reason"),
            transport_exception_expires_at=data.get("transport_exception_expires_at"),
            transport_approved_by=[],
            owner_user_id=actor.user_id,
            status="pending_approval" if exception else "active",
        )
    )
    await notify_change(conn, "connection.created", company_id, cid, actor)
    return cid


async def approve_exception(conn: AsyncConnection, actor: Actor, connection_id: uuid.UUID) -> str:
    """数据 Owner / 安全审批；生产连接需两名不同审批人（双人复核），其他环境一名。创建者不能审批。"""
    actor.require(actor.has("data_owner"), "只有数据 Owner / 安全可以审批传输例外")
    r = await row(conn, connection_id)
    if r["status"] != "pending_approval":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "该连接不在待审批状态")
    actor.require(actor.user_id != r["owner_user_id"], "不能审批自己登记的连接")
    approvers = list(r["transport_approved_by"] or [])
    if actor.user_id in approvers:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "同一审批人不能重复审批")
    approvers.append(actor.user_id)
    needed = 2 if r["env"] == "prod" else 1
    status = "active" if len(approvers) >= needed else "pending_approval"
    await conn.execute(
        update(system_connection)
        .where(system_connection.c.id == connection_id)
        .values(transport_approved_by=approvers, status=status, updated_at=utcnow())
    )
    await notify_change(conn, "connection.approved", r["company_id"], connection_id, actor, {"status": status})
    return status


async def expire_exceptions(conn: AsyncConnection) -> int:
    """到期未复审的传输例外自动禁用（随快照发布执行）。"""
    res = await conn.execute(
        update(system_connection)
        .where(
            and_(
                system_connection.c.status == "active",
                system_connection.c.transport_exception_expires_at.is_not(None),
                system_connection.c.transport_exception_expires_at <= utcnow(),
            )
        )
        .values(status="disabled")
    )
    return res.rowcount or 0


async def test_connection(
    conn: AsyncConnection, actor: Actor, connection_id: uuid.UUID, resolver: Resolver | None = None
) -> dict[str, Any]:
    actor.require(_manages(actor) or actor.has("data_owner"))
    r = await row(conn, connection_id)
    cfg = to_config(r)
    if cfg.status == "pending_approval":
        cfg.status = "active"  # 审批前也允许测试连通性（不携带凭据）
    result = await probe(cfg, allowlist=await allowlist(conn), resolver=resolver)
    await conn.execute(
        connection_test_run.insert().values(
            id=new_id(),
            connection_id=connection_id,
            company_id=r["company_id"],
            verdict=result["verdict"],
            error_code=result["error_code"],
            result=result,
            operator=actor.user_id,
        )
    )
    return result


async def draft_trust(conn: AsyncConnection, actor: Actor, connection_id: uuid.UUID) -> dict[str, Any]:
    actor.require(_manages(actor))
    last = (
        await conn.execute(
            select(connection_test_run.c.result)
            .where(connection_test_run.c.connection_id == connection_id)
            .order_by(connection_test_run.c.run_at.desc())
            .limit(1)
        )
    ).first()
    if last is None:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "请先执行连接测试")
    return trust_draft(last[0], "pinned")


async def create_connector_instance(
    conn: AsyncConnection,
    actor: Actor,
    *,
    name: str,
    kind: str,
    connection_id: uuid.UUID | None,
    config: dict[str, Any],
    concurrency: int = 8,
    rate_per_s: float = 20,
    burst: int = 40,
) -> uuid.UUID:
    actor.require(_manages(actor))
    company_id = (await row(conn, connection_id))["company_id"] if connection_id else None
    if company_id is None:
        if kind != "mock":
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "非 mock Connector 必须绑定系统连接")
        company_id = actor.company_id or uuid.UUID(int=0)
    iid = new_id()
    await conn.execute(
        connector_instance.insert().values(
            id=iid,
            name=name,
            company_id=company_id,
            kind=kind,
            system_connection_id=connection_id,
            config=config,
            concurrency=concurrency,
            rate_per_s=rate_per_s,
            burst=burst,
            status="active",
        )
    )
    await notify_change(conn, "connector.created", company_id, iid, actor, {"name": name})
    return iid


def check_tool_on_connection(spec: dict[str, Any], r: dict[str, Any]) -> None:
    """发布前置检查（附录 B T-GEN-N6）：传输例外连接上只允许服务账号身份。"""
    exception = r["scheme"] == "http" or r["tls_mode"] == "insecure"
    if exception and spec.get("identity_mode") != "service_account":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "http / insecure 连接上只允许 identity_mode=service_account")


async def check_release_connection(conn: AsyncConnection, spec: dict[str, Any], env: str) -> None:
    """发布 Tool 前检查其 Connector 绑定的连接（附录 B T-GEN-N6）。"""
    name = spec["connector_ref"]["connector"]
    inst = (await conn.execute(select(connector_instance).where(connector_instance.c.name == name))).mappings().first()
    if inst is None or inst["system_connection_id"] is None:
        return  # mock 或尚未登记实例：运行时找不到实例会直接拒绝
    check_tool_on_connection(spec, await row(conn, inst["system_connection_id"]))


async def _active_sql_templates(conn: AsyncConnection, instance_id: uuid.UUID) -> dict[str, Any]:
    rows = (
        await conn.execute(
            text(
                "SELECT template_key, engine, statement, param_schema, max_rows, "
                "statement_timeout_ms, explain_cost_cap, output_field_whitelist "
                "FROM conn.sql_query_template WHERE connector_instance_id = :iid AND status = 'active'"
            ),
            {"iid": instance_id},
        )
    ).mappings().all()
    out: dict[str, Any] = {}
    for r in rows:
        out[str(r["template_key"])] = {
            "engine": r["engine"],
            "statement": r["statement"],
            "param_schema": r["param_schema"] or {},
            "max_rows": r["max_rows"],
            "statement_timeout_ms": r["statement_timeout_ms"],
            "explain_cost_cap": float(r["explain_cost_cap"]) if r["explain_cost_cap"] is not None else None,
            "output_field_whitelist": list(r["output_field_whitelist"] or []),
        }
    return out


async def snapshot_part(conn: AsyncConnection) -> dict[str, Any]:
    """供发布快照使用：启用的 Connector 实例及其连接（只含凭据引用，不含明文）。"""
    await expire_exceptions(conn)
    conns = {r["id"]: r for r in (await conn.execute(select(system_connection))).mappings()}
    out = []
    for i in (await conn.execute(select(connector_instance).where(connector_instance.c.status == "active"))).mappings():
        c = conns.get(i["system_connection_id"])
        cfg = dict(i["config"] or {})
        if i["kind"] == "sql_readonly":
            cfg = {**cfg, "templates": await _active_sql_templates(conn, i["id"])}
        out.append(
            {
                "name": i["name"],
                "kind": i["kind"],
                "config": cfg,
                "concurrency": i["concurrency"],
                "rate_per_s": i["rate_per_s"],
                "burst": i["burst"],
                "connection": None
                if c is None
                else {
                    k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, dt.datetime) else v)
                    for k, v in c.items()
                    if k not in ("transport_approved_by", "owner_user_id", "updated_at")
                },
            }
        )
    allow = [str(c) for (c,) in (await conn.execute(select(network_allowlist.c.cidr))).all()]
    return {"connectors": sorted(out, key=lambda x: x["name"]), "network_allowlist": allow}
