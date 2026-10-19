"""M3 / P3：连接管理（传输例外审批与到期）、连接测试接口、Postgres 幂等、kb Connector、G1 契约测试（Prism Mock）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import httpx
import jsonschema
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from aip.control_plane.conn import service as conn_service
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import snapshot as release_snapshot
from aip.data_plane.runtime import PgExecutionStore
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from iam_seed import ROOT, Seed, actor_for, app_url
from m2_fixtures import KEY, promote, tool_spec
from m3_world import World, connection, connector, tool
from tls_fixtures import Server, make_cert

pytestmark = pytest.mark.infra
SYS = Actor.system_actor()
SOON = (utcnow() + dt.timedelta(days=30)).isoformat()


async def _resolve_local(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


# ---------------- 传输例外：审批、双人复核、到期禁用 ----------------


async def test_transport_exception_requires_dual_approval_in_prod(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    sec = await actor_for(db, seeded, "security")
    async with db.tx(DbScope.system()) as conn:
        other_sec = (
            await conn.execute(
                __import__("sqlalchemy").text("SELECT id FROM iam.iam_user WHERE login_name = 'vn_admin'")
            )
        ).scalar_one()
    async with db.tx(DbScope.system()) as conn:
        from aip.control_plane.iam import users

        await users.assign_role(conn, SYS, other_sec, "data_owner", seeded.group)
        sec2 = await users.load_actor(conn, other_sec)
        cid = await conn_service.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": "mes",
                "env": "prod",
                "base_url": "http://10.1.2.3:8080",
                "scheme": "http",
                "transport_exception_reason": "MES 只支持 HTTP",
                "transport_exception_expires_at": SOON,
            },
        )
        assert (await conn_service.row(conn, cid))["status"] == "pending_approval"
        with pytest.raises(PlatformError):
            await conn_service.approve_exception(conn, dev, cid)  # 不是数据 Owner
        assert await conn_service.approve_exception(conn, sec, cid) == "pending_approval"
        with pytest.raises(PlatformError, match="重复"):
            await conn_service.approve_exception(conn, sec, cid)
        assert await conn_service.approve_exception(conn, sec2, cid) == "active"


async def test_exception_rules_and_expiry(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as e:  # http 缺少例外理由与到期日
            await conn_service.create_connection(
                conn,
                dev,
                company_id=seeded.vn,
                data={"system_key": "wms", "env": "test", "base_url": "http://10.0.0.5", "scheme": "http"},
            )
        assert e.value.code == ErrorCode.INVALID_ARGUMENT
        with pytest.raises(PlatformError, match="90"):
            await conn_service.create_connection(
                conn,
                dev,
                company_id=seeded.vn,
                data={
                    "system_key": "wms",
                    "env": "test",
                    "base_url": "http://10.0.0.5",
                    "scheme": "http",
                    "transport_exception_reason": "x",
                    "transport_exception_expires_at": (utcnow() + dt.timedelta(days=200)).isoformat(),
                },
            )
        cid = await conn_service.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": "wms",
                "env": "test",
                "base_url": "http://10.0.0.5",
                "scheme": "http",
                "transport_exception_reason": "x",
                "transport_exception_expires_at": SOON,
            },
        )
        await conn.execute(
            update(conn_service.system_connection)
            .where(conn_service.system_connection.c.id == cid)
            .values(status="active", transport_exception_expires_at=utcnow() - dt.timedelta(1))
        )
        assert await conn_service.expire_exceptions(conn) == 1
        assert (await conn_service.row(conn, cid))["status"] == "disabled"


async def test_T_GEN_N6_user_delegated_tool_rejected_on_exception_connection(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_service.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": "hr",
                "env": "test",
                "base_url": "https://10.0.0.9",
                "scheme": "https",
                "tls_mode": "insecure",
                "transport_exception_reason": "自签",
                "transport_exception_expires_at": SOON,
            },
        )
        await conn_service.create_connector_instance(
            conn, dev, name="hr_vn01", kind="declarative", connection_id=cid, config={"operations": []}
        )
        aid = await registry.create_asset(
            conn, SYS, type_="tool", name="hr.leave", owner_org_id=seeded.hq, classification=2
        )
        spec = tool_spec(identity_mode="user_delegated", connector_ref={"connector": "hr_vn01", "operation": "x"})
        vid = await registry.create_version(conn, SYS, aid, "1.0.0", spec)
        await promote(conn, SYS, vid, "approved")
        with pytest.raises(PlatformError, match="service_account"):
            await registry.create_release(conn, SYS, vid, env="test", channel="stable", scope={"group": True})


async def test_connection_test_endpoint_and_trust_draft(db: Database, seeded: Seed) -> None:
    cert = make_cert()
    srv = Server(cert)
    dev = await actor_for(db, seeded, "admin")
    try:
        async with db.tx(DbScope.system()) as conn:
            cid = await conn_service.create_connection(
                conn,
                dev,
                company_id=seeded.vn,
                data={
                    "system_key": "qms",
                    "env": "test",
                    "base_url": srv.url(),
                    "scheme": "https",
                    "tls_mode": "system_ca",
                },
            )
            result = await conn_service.test_connection(conn, dev, cid, resolver=_resolve_local)
            assert result["verdict"] == "fail" and result["error_code"] == "TLS_UNTRUSTED"
            assert result["allowlisted"] is True and result["chain"][0]["self_signed"]
            draft = await conn_service.draft_trust(conn, dev, cid)
        assert draft["tls_mode"] == "pinned" and {p["kind"] for p in draft["pins"]} == {"spki", "cert"}
        assert srv.hits == 0, "连接测试不发 HTTP 请求、不带凭据"
    finally:
        srv.stop()


async def test_release_snapshot_carries_connectors_without_secrets(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_service.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": "mes",
                "env": "test",
                "base_url": "https://10.0.0.7",
                "scheme": "https",
                "tls_mode": "pinned",
                "pins": [{"kind": "spki", "sha256": "a" * 64}],
                "credential_ref": "kv/data/mes/vn01",
            },
        )
        await conn_service.create_connector_instance(
            conn, dev, name="mes_vn01", kind="declarative", connection_id=cid, config={"operations": []}
        )
        snap = await release_snapshot.publish(conn, KEY)
    assert snap is not None
    c = snap["payload"]["connectors"][0]
    assert c["name"] == "mes_vn01" and c["connection"]["credential_ref"] == "kv/data/mes/vn01"
    assert "127.0.0.0/8" in snap["payload"]["network_allowlist"]


# ---------------- Postgres 幂等：并发同键只执行一次 ----------------


async def test_pg_idempotency_under_concurrency(db: Database, tmp_path: pathlib.Path) -> None:
    from m3_world import write_tool

    store = PgExecutionStore(create_async_engine(app_url()))
    w = World(
        tmp_path,
        [write_tool("production.mes.report_output", "mock", "w")],
        [
            connector(
                "mock",
                "mock",
                {
                    "operations": {
                        "w": {"response": {"ok": 1}, "write": True, "delay_s": 0.2},
                        "query": {"response": {}},
                    }
                },
            )
        ],
        store=store,
    )
    await w.ready()
    p = w.principal()
    results = await asyncio.gather(
        *[
            w.runtime.invoke(p, "production.mes.report_output", {"factory_id": "F001"}, idempotency_key="pg-1")
            for _ in range(5)
        ]
    )
    ids = {r["execution_id"] for r in results}
    assert len(ids) == 1, "同一幂等键并发只产生一条执行记录"
    assert w.connector("mock").calls["w"] == 1
    rec = await store.get(ids.pop())
    assert rec is not None and rec["idempotency_key"] == "pg-1"
    await store.engine.dispose()


# ---------------- kb Connector ----------------


async def test_kb_connector_search(tmp_path: pathlib.Path) -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Kb(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.headers["X-API-Key"] == "kb-key-vn01"
            body = json.dumps(
                {
                    "items": [
                        {
                            "id": "d1",
                            "title": "注塑工艺规范",
                            "url": "http://kb/d1",
                            "snippet": "保压时间",
                            "acl": "internal",
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Kb)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        spec = tool(
            "knowledge.kb.search",
            "kb_vn01",
            "search",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            output_schema={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "snippet": {"type": "string"},
                    },
                },
            },
            scope_binding={},
        )
        conn = connection(f"http://127.0.0.1:{httpd.server_address[1]}", credential_ref="kv/data/kb/vn01")
        w = World(
            tmp_path,
            [spec],
            [connector("kb_vn01", "kb", {"auth": {"mode": "api_key"}}, conn)],
            secrets={"kv/data/kb/vn01": {"api_key": "kb-key-vn01"}},
        )
        await w.ready()
        r = await w.runtime.invoke(w.principal(), "knowledge.kb.search", {"query": "保压"})
        assert r["status"] == "succeeded", (r["error"], r["next_action"])
        assert r["data"] == [
            {"document_id": "d1", "title": "注塑工艺规范", "url": "http://kb/d1", "snippet": "保压时间"}
        ]
    finally:
        httpd.shutdown()


# ---------------- G1 契约测试：声明式 Connector 对 Prism Mock ----------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def prism() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    port = _free_port()
    spec_dir = ROOT / "tests" / "contracts"
    name = f"aip-prism-{port}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:4010",
            "-v",
            f"{spec_dir}:/specs",
            "stoplight/prism:5.12.0",
            "mock",
            "-h",
            "0.0.0.0",
            "/specs/mes-vn01.openapi.yaml",
        ],
        check=True,
        capture_output=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/api/v2/workorders?plant=F001", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


async def test_G1_declarative_connector_against_prism_mock(prism: str, tmp_path: pathlib.Path) -> None:
    """G1：按旧系统 OpenAPI 起 Mock，声明式映射的请求被 Prism 校验通过，返回值符合 Tool 的 output_schema。"""
    mapping = {
        "operations": [
            {
                "connector": "mes_vn01",
                "operation": "query_work_orders",
                "request": {
                    "method": "GET",
                    "path": "/api/v2/workorders",
                    "query": {
                        "plant": "{{ args.factory_id }}",
                        "status": "{{ args.status | default('') }}",
                        "pageSize": "{{ args.limit | default(50) | max(200) }}",
                    },
                },
                "response": {
                    "success_when": "$.code == 0",
                    "data": "$.data.items[*]",
                    "fields": {"wo_no": "$.woNo", "qty": "$.planQty", "status": "$.status"},
                    "next_cursor": "$.data.nextPage",
                },
            }
        ]
    }
    spec = tool(
        "production.mes.query_work_orders",
        "mes_vn01",
        "query_work_orders",
        input_schema={
            "type": "object",
            "properties": {
                "factory_id": {"type": "string"},
                "status": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["factory_id"],
        },
        output_schema={
            "type": "array",
            "items": {
                "type": "object",
                "required": ["wo_no", "qty", "status"],
                "properties": {"wo_no": {"type": "string"}, "qty": {"type": "integer"}, "status": {"type": "string"}},
            },
        },
        timeout={"connect_ms": 2000, "read_ms": 5000, "deadline_ms": 8000},
    )
    w = World(tmp_path, [spec], [connector("mes_vn01", "declarative", mapping, connection(prism))])
    await w.ready()
    r = await w.runtime.invoke(
        w.principal(), "production.mes.query_work_orders", {"factory_id": "F001", "status": "open", "limit": 500}
    )
    assert r["status"] == "succeeded", r
    jsonschema.validate(r["data"], spec["output_schema"])
    assert r["data"], "Mock 返回了示例数据"
    bad = await w.runtime.invoke(
        w.principal(), "production.mes.query_work_orders", {"factory_id": "F001", "limit": "x"}
    )
    assert bad["error"]["code"] == "INVALID_ARGUMENT"


def test_contract_file_exists() -> None:
    assert (ROOT / "tests" / "contracts" / "mes-vn01.openapi.yaml").exists()


async def _unused(_: Any) -> None:  # pragma: no cover - 保持导入被使用
    _ = (Seed,)
