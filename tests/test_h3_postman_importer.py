"""H3：Postman Collection Importer → 声明式 Connector / Tool 草稿。"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.conn import service as conn_svc
from aip.control_plane.db import Database, DbScope
from aip.control_plane.importer import postman as pm
from aip.control_plane.importer.service import import_postman
from aip.control_plane.registry import service as registry
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import PASSWORD, Seed, actor_for, app_url

ROOT = Path(__file__).resolve().parents[1]
MES = (ROOT / "tests" / "contracts" / "mes-vn01.postman_collection.json").read_text(encoding="utf-8")


async def _login(cp: httpx.AsyncClient, seed: Seed, name: str = "admin") -> dict[str, str]:
    r = await cp.post(
        "/api/v1/auth/login",
        json={"login_name": name, "password": PASSWORD, "totp": seed.code(name)},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


@pytest.mark.asyncio
async def test_preview_mes_vn01_collection(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        out = await import_postman(
            conn,
            dev,
            document=MES,
            connector_name="mes_pm01",
            domain="production",
            system="mes",
            persist=False,
        )
    ops = out["connector_config"]["operations"]
    assert len(ops) >= 1
    op = next(o for o in ops if o["operation"] == "list_work_orders")
    assert op["request"]["path"] == "/api/v2/workorders"
    q = op["request"]["query"]
    assert "plant" in q and "{{ args.plant }}" in q["plant"]
    assert "status" in q
    assert op["response"]["success_when"] == "$.code == 0"
    assert op["response"]["data"] == "$.data.items[*]"
    assert op["response"]["fields"]["wo_no"] == "$.woNo"
    tool = next(t for t in out["tools"] if t["capability_id"] == "production.mes.list_work_orders")
    assert tool["risk_level"] == "read"
    assert tool["connector_ref"]["connector"] == "mes_pm01"


@pytest.mark.asyncio
async def test_persist_postman_drafts(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_svc.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": "mes",
                "env": "test",
                "base_url": "https://10.0.0.72",
                "scheme": "https",
                "tls_mode": "pinned",
                "pins": [{"kind": "spki", "sha256": "c" * 64}],
            },
        )
        out = await import_postman(
            conn,
            dev,
            document=MES,
            connector_name="mes_pm02",
            domain="production",
            system="mes",
            persist=True,
            connection_id=cid,
            owner_org_id=seeded.hq,
        )
        assert out["connector_instance_id"] and out["drafts"]
        inst = (
            await conn.execute(
                select(conn_svc.connector_instance).where(conn_svc.connector_instance.c.name == "mes_pm02")
            )
        ).mappings().first()
        assert inst is not None and inst["kind"] == "declarative"
        for d in out["drafts"]:
            v = await registry.version_row(conn, uuid.UUID(d["version_id"]))
            assert v["state"] == "draft"


@pytest.mark.asyncio
async def test_invalid_postman_document(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as ei:
            await import_postman(conn, dev, document="{", connector_name="mes_bad")
        assert ei.value.code == ErrorCode.INVALID_ARGUMENT
        with pytest.raises(PlatformError) as ei2:
            await import_postman(
                conn, dev, document='{"info": {}, "item": []}', connector_name="mes_bad"
            )
        assert ei2.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_import_postman_http_route(db: Database, seeded: Seed) -> None:
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        headers = await _login(cp, seeded, "admin")
        r = await cp.post(
            "/api/v1/importer/postman",
            headers=headers,
            json={
                "document": MES,
                "connector_name": "mes_pm03",
                "domain": "production",
                "system": "mes",
                "persist": False,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["connector_config"]["operations"]
        bad = await cp.post(
            "/api/v1/importer/postman",
            headers=headers,
            json={"document": "[]", "connector_name": "mes_pm03"},
        )
        assert bad.status_code == 400
        assert bad.json()["code"] == "INVALID_ARGUMENT"


def test_skips_formdata() -> None:
    doc = pm.load_collection(
        """
{
  "info": {"name": "t", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
  "item": [
    {
      "name": "upload",
      "request": {"method": "POST", "body": {"mode": "formdata", "formdata": []}, "url": "/up"}
    },
    {
      "name": "getY",
      "request": {"method": "GET", "url": {"path": ["y"]}},
      "response": [{"code": 200, "body": "{\\"a\\": 1}"}]
    }
  ]
}
"""
    )
    ops, warnings = pm.parse_operations(doc)
    assert len(ops) == 1 and ops[0]["operation"] == "get_y"
    assert any("formdata" in w for w in warnings)
