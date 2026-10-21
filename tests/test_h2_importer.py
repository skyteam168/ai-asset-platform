"""H2：OpenAPI Importer → 声明式 Connector / Tool 草稿。"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.conn import service as conn_svc
from aip.control_plane.db import Database, DbScope
from aip.control_plane.importer import openapi as oa
from aip.control_plane.importer.service import import_openapi
from aip.control_plane.registry import service as registry
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import PASSWORD, Seed, actor_for, app_url

ROOT = Path(__file__).resolve().parents[1]
MES = (ROOT / "tests" / "contracts" / "mes-vn01.openapi.yaml").read_text(encoding="utf-8")


async def _login(cp: httpx.AsyncClient, seed: Seed, name: str = "admin") -> dict[str, str]:
    r = await cp.post(
        "/api/v1/auth/login",
        json={"login_name": name, "password": PASSWORD, "totp": seed.code(name)},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


@pytest.mark.asyncio
async def test_preview_mes_vn01(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        out = await import_openapi(
            conn,
            dev,
            document=MES,
            connector_name="mes_imp01",
            domain="production",
            system="mes",
            persist=False,
        )
    ops = out["connector_config"]["operations"]
    assert len(ops) >= 1
    op = next(o for o in ops if o["operation"] == "list_work_orders")
    q = op["request"]["query"]
    assert "plant" in q and "{{ args.plant }}" in q["plant"]
    assert "status" in q
    assert "pageSize" in q and "max(200)" in q["pageSize"] and "default(50)" in q["pageSize"]
    assert op["response"]["success_when"] == "$.code == 0"
    assert op["response"]["data"] == "$.data.items[*]"
    assert op["response"]["fields"]["wo_no"] == "$.woNo"
    assert "plan_qty" in op["response"]["fields"]
    tool = next(t for t in out["tools"] if t["capability_id"] == "production.mes.list_work_orders")
    assert tool["risk_level"] == "read"
    assert tool["connector_ref"] == {"connector": "mes_imp01", "operation": "list_work_orders"}
    assert "plant" in tool["input_schema"]["properties"]


@pytest.mark.asyncio
async def test_persist_creates_connector_and_draft(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_svc.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": "mes",
                "env": "test",
                "base_url": "https://10.0.0.71",
                "scheme": "https",
                "tls_mode": "pinned",
                "pins": [{"kind": "spki", "sha256": "b" * 64}],
            },
        )
        out = await import_openapi(
            conn,
            dev,
            document=MES,
            connector_name="mes_imp02",
            domain="production",
            system="mes",
            persist=True,
            connection_id=cid,
            owner_org_id=seeded.hq,
        )
        assert out["connector_instance_id"]
        assert out["drafts"]
        inst = (
            await conn.execute(
                select(conn_svc.connector_instance).where(conn_svc.connector_instance.c.name == "mes_imp02")
            )
        ).mappings().first()
        assert inst is not None and inst["kind"] == "declarative"
        assert inst["config"]["operations"]
        for d in out["drafts"]:
            v = await registry.version_row(conn, uuid.UUID(d["version_id"]))
            assert v["state"] == "draft"
            assert d["state"] == "draft"


@pytest.mark.asyncio
async def test_invalid_document_raises(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as ei:
            await import_openapi(conn, dev, document="not-yaml: [", connector_name="mes_bad")
        assert ei.value.code == ErrorCode.INVALID_ARGUMENT
        with pytest.raises(PlatformError) as ei2:
            await import_openapi(conn, dev, document="openapi: '2.0'\npaths: {}", connector_name="mes_bad")
        assert ei2.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_import_openapi_http_route(db: Database, seeded: Seed) -> None:
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        headers = await _login(cp, seeded, "admin")
        r = await cp.post(
            "/api/v1/importer/openapi",
            headers=headers,
            json={
                "document": MES,
                "connector_name": "mes_imp03",
                "domain": "production",
                "system": "mes",
                "persist": False,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connector_config"]["operations"]
        assert body["tools"]
        bad = await cp.post(
            "/api/v1/importer/openapi",
            headers=headers,
            json={"document": "{", "connector_name": "mes_imp03"},
        )
        assert bad.status_code == 400
        assert bad.json()["code"] == "INVALID_ARGUMENT"


def test_parse_skips_non_json() -> None:
    doc = oa.load_document(
        """
openapi: 3.0.3
info: {title: t, version: "1"}
paths:
  /x:
    get:
      responses:
        "200":
          description: ok
          content:
            text/plain:
              schema: {type: string}
  /y:
    get:
      operationId: getY
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {type: object, properties: {a: {type: string}}}
"""
    )
    ops, warnings = oa.parse_operations(doc)
    assert len(ops) == 1 and ops[0]["operation"] == "get_y"
    assert any("非 JSON" in w for w in warnings)
