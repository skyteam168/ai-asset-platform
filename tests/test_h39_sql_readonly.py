"""H39：sql_readonly 连接、模板与 PostgreSQL 执行（ADR-0026 / architecture 9.5）。"""

from __future__ import annotations

import datetime as dt
import time
import uuid

import pytest
from sqlalchemy import text

from aip.connectors.base import ExecContext
from aip.connectors.credentials import MemorySecretStore
from aip.connectors.sql_readonly import SqlReadonlyConnector, assert_readonly_sql, bind_named
from aip.control_plane.conn import service as conn_svc
from aip.control_plane.db import Database, DbScope
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from iam_seed import Seed, actor_for


async def _pg_conn(db: Database, seeded: Seed, *, system_key: str = "erp_ro") -> uuid.UUID:
    """本地 compose PG 无 TLS：用 insecure + 强制 active（跳过传输例外双人审批）。"""
    dev = await actor_for(db, seeded, "admin")
    expires = (utcnow() + dt.timedelta(days=30)).isoformat()
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_svc.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": system_key,
                "env": "test",
                "base_url": "postgres://127.0.0.1:55432/aip",
                "scheme": "postgres",
                "tls_mode": "insecure",
                "credential_ref": "kv/data/sql/erp_ro",
                "transport_exception_reason": "dev sql_readonly test",
                "transport_exception_expires_at": expires,
            },
        )
        await conn.execute(
            text("UPDATE conn.system_connection SET status='active' WHERE id=:id"),
            {"id": cid},
        )
        return cid


@pytest.mark.asyncio
async def test_create_postgres_connection_and_sql_instance(db: Database, seeded: Seed) -> None:
    cid = await _pg_conn(db, seeded, system_key="erp_ro_a")
    async with db.tx(DbScope.system()) as conn:
        row = await conn_svc.row(conn, cid)
        assert row["scheme"] == "postgres"
        assert row["tls_mode"] == "insecure"
        assert row["status"] == "active"
        dev = await actor_for(db, seeded, "admin")
        iid = await conn_svc.create_connector_instance(
            conn,
            dev,
            name="erp_sql_a",
            kind="sql_readonly",
            connection_id=cid,
            config={},
        )
        assert iid


@pytest.mark.asyncio
async def test_https_connection_unchanged(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_svc.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": "mes_h39",
                "env": "test",
                "base_url": "https://10.0.0.71",
                "scheme": "https",
                "tls_mode": "pinned",
                "pins": [{"kind": "spki", "sha256": "c" * 64}],
            },
        )
        assert (await conn_svc.row(conn, cid))["status"] == "active"


def test_build_client_rejects_postgres() -> None:
    from aip.libs.httpclient import build_client
    from aip.libs.httpclient.config import ConnectionConfig

    cfg = ConnectionConfig(
        id="x",
        base_url="postgres://127.0.0.1:5432/db",
        scheme="postgres",
        tls_mode="system_ca",
        status="active",
    )
    with pytest.raises(PlatformError) as ei:
        build_client(cfg)
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_sql_rejects_multi_statement() -> None:
    with pytest.raises(PlatformError) as ei:
        assert_readonly_sql("SELECT 1; DROP TABLE x")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_sql_rejects_non_select() -> None:
    with pytest.raises(PlatformError) as ei:
        assert_readonly_sql("DELETE FROM t WHERE id = :id")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_sql_template_api_create_activate_try(db: Database, seeded: Seed) -> None:
    import httpx

    from aip.control_plane.app import create_app as create_cp
    from iam_seed import PASSWORD

    cid = await _pg_conn(db, seeded, system_key="erp_ro_api")
    async with db.tx(DbScope.system()) as conn:
        dev = await actor_for(db, seeded, "admin")
        iid = await conn_svc.create_connector_instance(
            conn, dev, name="erp_sql_api", kind="sql_readonly", connection_id=cid, config={}
        )
    app = create_cp(db)
    app.state.secrets = MemorySecretStore({"kv/data/sql/erp_ro": {"username": "aip", "password": "aip-dev-only"}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        login = await cp.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": PASSWORD, "totp": seeded.code("admin")},
        )
        assert login.status_code == 200, login.text
        h = {"Authorization": f"Bearer {login.json()['session_token']}"}
        created = await cp.post(
            f"/api/v1/connector-instances/{iid}/sql-templates",
            headers=h,
            json={
                "template_key": "ping_one",
                "engine": "postgres",
                "statement": "SELECT 1 AS n WHERE :flag = 1",
                "param_schema": {"type": "object", "properties": {"flag": {"type": "integer"}}},
                "max_rows": 10,
                "explain_cost_cap": 1_000_000,
            },
        )
        assert created.status_code == 201, created.text
        tid = created.json()["id"]
        tried = await cp.post(f"/api/v1/sql-templates/{tid}/try", headers=h, json={"params": {"flag": 1}})
        assert tried.status_code == 200, tried.text
        assert tried.json()["row_count"] >= 1
        act = await cp.post(f"/api/v1/sql-templates/{tid}/activate", headers=h)
        assert act.status_code == 200, act.text
        assert act.json()["status"] == "active"


@pytest.mark.asyncio
async def test_sql_template_select_ok() -> None:
    secrets = MemorySecretStore({"kv/data/sql/erp_ro": {"username": "aip", "password": "aip-dev-only"}})
    connector = SqlReadonlyConnector(
        "erp_sql",
        {
            "connection": {
                "scheme": "postgres",
                "base_url": "postgres://127.0.0.1:55432/aip",
                "tls_mode": "insecure",
                "credential_ref": "kv/data/sql/erp_ro",
                "connect_timeout_s": 3,
            },
            "templates": {
                "ping_one": {
                    "engine": "postgres",
                    "statement": "SELECT 1 AS n WHERE :flag = 1",
                    "max_rows": 10,
                    "statement_timeout_ms": 3000,
                    "output_field_whitelist": ["n"],
                }
            },
        },
        secrets,
    )
    ctx = ExecContext(trace_id="t", deadline=time.monotonic() + 10, mode="live_read")
    result = await connector.execute("ping_one", {"flag": 1}, ctx)
    assert result.data == [{"n": 1}]


def test_bind_named_mysql_style() -> None:
    sql, args = bind_named("SELECT :a AS x WHERE :b = 1 AND :a > 0", {"a": 2, "b": 1}, style="mysql")
    assert sql == "SELECT %s AS x WHERE %s = 1 AND %s > 0"
    assert args == [2, 1, 2]


def test_build_client_rejects_mysql() -> None:
    from aip.libs.httpclient import build_client
    from aip.libs.httpclient.config import ConnectionConfig

    cfg = ConnectionConfig(
        id="x",
        base_url="mysql://127.0.0.1:3306/db",
        scheme="mysql",
        tls_mode="system_ca",
        status="active",
    )
    with pytest.raises(PlatformError) as ei:
        build_client(cfg)
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.mysql
@pytest.mark.asyncio
async def test_mysql_template_select_ok() -> None:
    """需 `pip install 'aip[mysql]'` 且设置 AIP_TEST_MYSQL_URL=mysql://host:3306/db，凭据见下方。"""
    import os

    url = os.environ.get("AIP_TEST_MYSQL_URL")
    if not url:
        pytest.skip("未设置 AIP_TEST_MYSQL_URL")
    try:
        import asyncmy  # noqa: F401
    except ImportError:
        pytest.skip("未安装 asyncmy（optional-dependencies mysql）")

    user = os.environ.get("AIP_TEST_MYSQL_USER", "aip")
    password = os.environ.get("AIP_TEST_MYSQL_PASSWORD", "aip-dev-only")
    secrets = MemorySecretStore({"kv/data/sql/mysql_ro": {"username": user, "password": password}})
    connector = SqlReadonlyConnector(
        "erp_mysql",
        {
            "connection": {
                "scheme": "mysql",
                "base_url": url,
                "tls_mode": "insecure",
                "credential_ref": "kv/data/sql/mysql_ro",
                "connect_timeout_s": 3,
            },
            "templates": {
                "ping_one": {
                    "engine": "mysql",
                    "statement": "SELECT 1 AS n WHERE :flag = 1",
                    "max_rows": 10,
                    "statement_timeout_ms": 3000,
                    "output_field_whitelist": ["n"],
                }
            },
        },
        secrets,
    )
    ctx = ExecContext(trace_id="t", deadline=time.monotonic() + 10, mode="live_read")
    result = await connector.execute("ping_one", {"flag": 1}, ctx)
    assert result.data == [{"n": 1}]
