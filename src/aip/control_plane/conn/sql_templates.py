"""SQL 只读模板登记 / 试跑 / 激活（architecture.md 9.5、ADR-0026）。"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.connectors.base import ExecContext
from aip.connectors.credentials import SecretStore
from aip.connectors.sql_readonly import SqlReadonlyConnector, assert_readonly_sql, bind_named
from aip.control_plane.conn import service as conn_svc
from aip.control_plane.iam.authz import Actor
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.dbclient.pool import open_connection


def _manage(actor: Actor) -> None:
    actor.require(actor.has("platform_admin") or actor.has("asset_developer"), "需要平台管理员或资产开发者")


async def _instance(conn: AsyncConnection, instance_id: uuid.UUID) -> Any:
    row = (
        await conn.execute(
            text("SELECT * FROM conn.connector_instance WHERE id = :id"),
            {"id": instance_id},
        )
    ).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "Connector 实例不存在")
    if row["kind"] != "sql_readonly":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅 sql_readonly 实例可登记 SQL 模板")
    return row


async def create_template(
    conn: AsyncConnection,
    actor: Actor,
    *,
    instance_id: uuid.UUID,
    body: dict[str, Any],
) -> dict[str, Any]:
    _manage(actor)
    inst = await _instance(conn, instance_id)
    assert_readonly_sql(str(body["statement"]))
    if inst.get("system_connection_id"):
        crow = await conn_svc.row(conn, inst["system_connection_id"])
        if crow["scheme"] != body["engine"]:
            raise PlatformError(
                ErrorCode.INVALID_ARGUMENT,
                f"模板引擎 {body['engine']} 与连接 scheme {crow['scheme']} 不一致",
            )
    tid = new_id()
    await conn.execute(
        text(
            """
            INSERT INTO conn.sql_query_template (
              id, company_id, connector_instance_id, template_key, engine, statement,
              param_schema, max_rows, statement_timeout_ms, explain_cost_cap,
              output_field_whitelist, status, created_at, updated_at
            ) VALUES (
              :id, :company_id, :iid, :key, :engine, :statement,
              CAST(:param_schema AS jsonb), :max_rows, :timeout, :cost,
              CAST(:whitelist AS jsonb), 'draft', :now, :now
            )
            """
        ),
        {
            "id": tid,
            "company_id": inst["company_id"],
            "iid": instance_id,
            "key": body["template_key"],
            "engine": body["engine"],
            "statement": body["statement"].strip(),
            "param_schema": json.dumps(body.get("param_schema") or {}),
            "max_rows": int(body.get("max_rows") or 200),
            "timeout": int(body.get("statement_timeout_ms") or 5000),
            "cost": body.get("explain_cost_cap"),
            "whitelist": json.dumps(body.get("output_field_whitelist") or []),
            "now": utcnow(),
        },
    )
    return await get_template(conn, tid)


async def list_templates(conn: AsyncConnection, actor: Actor, instance_id: uuid.UUID) -> list[dict[str, Any]]:
    _manage(actor)
    await _instance(conn, instance_id)
    rows = (
        await conn.execute(
            text(
                "SELECT * FROM conn.sql_query_template WHERE connector_instance_id = :iid "
                "ORDER BY template_key"
            ),
            {"iid": instance_id},
        )
    ).mappings().all()
    return [_out(r) for r in rows]


async def get_template(conn: AsyncConnection, template_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await conn.execute(text("SELECT * FROM conn.sql_query_template WHERE id = :id"), {"id": template_id})
    ).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "SQL 模板不存在")
    return _out(row)


async def update_template(
    conn: AsyncConnection, actor: Actor, template_id: uuid.UUID, body: dict[str, Any]
) -> dict[str, Any]:
    _manage(actor)
    cur = await get_template(conn, template_id)
    if "statement" in body:
        assert_readonly_sql(str(body["statement"]))
    fields = {
        "statement": body.get("statement", cur["statement"]),
        "param_schema": body.get("param_schema", cur["param_schema"]),
        "max_rows": int(body.get("max_rows", cur["max_rows"])),
        "statement_timeout_ms": int(body.get("statement_timeout_ms", cur["statement_timeout_ms"])),
        "explain_cost_cap": body.get("explain_cost_cap", cur.get("explain_cost_cap")),
        "output_field_whitelist": body.get("output_field_whitelist", cur.get("output_field_whitelist") or []),
        "engine": body.get("engine", cur["engine"]),
    }
    await conn.execute(
        text(
            """
            UPDATE conn.sql_query_template SET
              statement = :statement,
              param_schema = CAST(:param_schema AS jsonb),
              max_rows = :max_rows,
              statement_timeout_ms = :timeout,
              explain_cost_cap = :cost,
              output_field_whitelist = CAST(:whitelist AS jsonb),
              engine = :engine,
              status = 'draft',
              updated_at = :now
            WHERE id = :id
            """
        ),
        {
            "id": template_id,
            "statement": fields["statement"],
            "param_schema": json.dumps(fields["param_schema"]),
            "max_rows": fields["max_rows"],
            "timeout": fields["statement_timeout_ms"],
            "cost": fields["explain_cost_cap"],
            "whitelist": json.dumps(fields["output_field_whitelist"]),
            "engine": fields["engine"],
            "now": utcnow(),
        },
    )
    return await get_template(conn, template_id)


async def explain_cost(conn: AsyncConnection, secrets: SecretStore, template: dict[str, Any]) -> float | None:
    """PostgreSQL EXPLAIN Total Cost；MySQL 首期返回 None。"""
    if template["engine"] != "postgres":
        return None
    inst = await _instance(conn, uuid.UUID(template["connector_instance_id"]))
    crow = await conn_svc.row(conn, inst["system_connection_id"])
    ref = crow.get("credential_ref")
    if not ref:
        raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "数据库连接未配置 credential_ref")
    secret = await secrets.get(str(ref))
    statement = assert_readonly_sql(template["statement"])
    raw = await open_connection(
        scheme=str(crow["scheme"]),
        base_url=str(crow["base_url"]),
        tls_mode=crow.get("tls_mode"),
        ca_pem=crow.get("ca_pem"),
        username=secret.get("username") or secret.get("user") or "",
        password=secret.get("password") or "",
    )
    try:
        params = {m: 1 for m in re.findall(r":([a-zA-Z_][a-zA-Z0-9_]*)", statement)}
        sql, args = bind_named(statement, params)
        plan = await raw.fetch(f"EXPLAIN (FORMAT JSON) {sql}", *args)
        doc = plan[0][0]
        if isinstance(doc, str):
            doc = json.loads(doc)
        return float(doc[0]["Plan"]["Total Cost"])
    finally:
        await raw.close()


async def activate_template(
    conn: AsyncConnection, actor: Actor, secrets: SecretStore, template_id: uuid.UUID
) -> dict[str, Any]:
    _manage(actor)
    tpl = await get_template(conn, template_id)
    cap = tpl.get("explain_cost_cap")
    if cap is not None and tpl["engine"] == "postgres":
        cost = await explain_cost(conn, secrets, tpl)
        if cost is not None and cost > float(cap):
            raise PlatformError(
                ErrorCode.INVALID_ARGUMENT,
                f"EXPLAIN 成本 {cost} 超过上限 {cap}",
            )
    await conn.execute(
        text("UPDATE conn.sql_query_template SET status='active', updated_at=:now WHERE id=:id"),
        {"id": template_id, "now": utcnow()},
    )
    return await get_template(conn, template_id)


async def try_template(
    conn: AsyncConnection,
    actor: Actor,
    secrets: SecretStore,
    template_id: uuid.UUID,
    params: dict[str, Any],
) -> dict[str, Any]:
    _manage(actor)
    tpl = await get_template(conn, template_id)
    inst = await _instance(conn, uuid.UUID(tpl["connector_instance_id"]))
    crow = await conn_svc.row(conn, inst["system_connection_id"])
    connector = SqlReadonlyConnector(
        inst["name"],
        {
            "connection": {
                k: (str(v) if isinstance(v, uuid.UUID) else v)
                for k, v in crow.items()
                if k
                in (
                    "scheme",
                    "base_url",
                    "tls_mode",
                    "ca_pem",
                    "credential_ref",
                    "connect_timeout_s",
                )
            },
            "templates": {
                tpl["template_key"]: {
                    "engine": tpl["engine"],
                    "statement": tpl["statement"],
                    "max_rows": min(int(tpl["max_rows"]), 20),
                    "statement_timeout_ms": tpl["statement_timeout_ms"],
                    "output_field_whitelist": tpl.get("output_field_whitelist") or [],
                }
            },
        },
        secrets,
    )
    ctx = ExecContext(trace_id="try", deadline=time.monotonic() + 30, mode="live_read")
    result = await connector.execute(tpl["template_key"], params, ctx)
    rows = result.data if isinstance(result.data, list) else []
    return {"row_count": len(rows), "sample": rows[:5]}


def _out(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k, v in list(d.items()):
        if isinstance(v, uuid.UUID):
            d[k] = str(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d
