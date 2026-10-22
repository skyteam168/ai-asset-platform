"""控制台补充接口：版本列表与详情、授权上限与使用申请列表、审批摘要、审计检索与校验、静态托管。"""

from __future__ import annotations

import datetime as dt
import pathlib

import httpx
import pytest

from aip.control_plane import app as cp_module
from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.libs.audit.spool import AuditSpool
from aip.libs.audit.store import anchor, ship
from aip.libs.tokens.keys import platform_key
from iam_seed import PASSWORD, Seed, app_url
from m2_fixtures import Published, publish_tool

pytestmark = pytest.mark.infra
V = "/api/v1"


@pytest.fixture
async def cp(seeded: Seed) -> httpx.AsyncClient:
    app = create_cp(db=Database(app_url()))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal")


@pytest.fixture
async def published(db: Database, seeded: Seed) -> Published:
    return await publish_tool(db, seeded)


async def login(cp: httpx.AsyncClient, seed: Seed, name: str) -> dict[str, str]:
    r = await cp.post(f"{V}/auth/login", json={"login_name": name, "password": PASSWORD, "totp": seed.code(name)})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


async def test_assets_and_catalog_list_summary(
    cp: httpx.AsyncClient, db: Database, seeded: Seed, published: Published
) -> None:
    from aip.control_plane.conn import service as conn_service
    from aip.control_plane.registry import snapshot as release_snapshot
    from iam_seed import actor_for
    from m2_fixtures import KEY

    admin = await login(cp, seeded, "admin")
    listed = (await cp.get(f"{V}/assets", headers=admin)).json()["items"]
    hit = next(x for x in listed if x["id"] == str(published.asset_id))
    assert "生产工单" in hit["description"]
    assert hit["connector"] == "mes"
    assert hit["latest_state"] == "active"
    assert hit["latest_semver"] == "1.0.0"
    assert hit["risk_level"] == "read"
    assert hit["title"] == "query_work_orders"

    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_service.create_connection(
            conn,
            dev,
            company_id=seeded.vn,
            data={
                "system_key": "mes",
                "env": "test",
                "base_url": "https://mes.example.internal:8443/api",
                "scheme": "https",
                "tls_mode": "pinned",
                "pins": [{"kind": "spki", "sha256": "a" * 64}],
                "credential_ref": "kv/data/mes/vn01",
            },
        )
        await conn_service.create_connector_instance(
            conn, dev, name="mes", kind="declarative", connection_id=cid, config={"operations": []}
        )
        await release_snapshot.publish(conn, KEY)

    listed2 = (await cp.get(f"{V}/assets", headers=admin)).json()["items"]
    hit2 = next(x for x in listed2 if x["id"] == str(published.asset_id))
    assert hit2["system_key"] == "mes"
    assert hit2["system_host"] == "mes.example.internal:8443"

    cat = (await cp.get(f"{V}/console/catalog", headers=admin)).json()
    tool = next(x for x in cat["items"] if x["capability_id"] == "production.mes.query_work_orders")
    assert "生产工单" in tool["description"]
    assert tool["connector"] == "mes"
    assert tool["system_key"] == "mes"
    assert tool["system_host"] == "mes.example.internal:8443"
    assert tool["asset_id"] == str(published.asset_id)
    assert tool["company_id"]
    assert tool["owner_org_id"]


async def test_versions_list_and_detail(cp: httpx.AsyncClient, seeded: Seed, published: Published) -> None:
    admin = await login(cp, seeded, "admin")
    r = await cp.get(f"{V}/assets/{published.asset_id}/versions", headers=admin)
    assert r.status_code == 200, r.text
    [v] = r.json()["items"]
    assert v["semver"] == "1.0.0" and v["state"] == "active" and v["content_hash"].startswith("sha256:")

    d = (await cp.get(f"{V}/versions/{v['id']}", headers=admin)).json()
    assert d["spec"]["capability_id"] and d["test_cases"] == [] and d["acceptance"] is None

    missing = await cp.get(f"{V}/versions/00000000-0000-0000-0000-000000000000", headers=admin)
    assert missing.status_code == 404


async def test_ceilings_usage_and_approval_subject(cp: httpx.AsyncClient, seeded: Seed, published: Published) -> None:
    admin = await login(cp, seeded, "admin")
    vn = await login(cp, seeded, "vn_admin")
    kh = await login(cp, seeded, "kh_admin")

    vn_ceilings = (await cp.get(f"{V}/grant-ceilings", headers=vn)).json()["items"]
    assert [c["id"] for c in vn_ceilings] == [str(published.ceiling_vn)]
    assert vn_ceilings[0]["asset_ids"] == [str(published.asset_id)]
    assert (await cp.get(f"{V}/grant-ceilings", headers=kh)).json()["items"] == []  # RLS：柬埔寨看不到越南的上限

    # 平台管理员替越南消费方申请（审批人是越南分公司管理员，不能是申请人）
    body = {
        "consumer_id": str(seeded.consumers["vn-ai"]),
        "asset_id": str(published.asset_id),
        "env": "staging",
        "reason": "质量看板接入",
    }
    r = await cp.post(f"{V}/usage-requests", headers=admin, json=body)
    assert r.status_code == 201, r.text
    listed = (await cp.get(f"{V}/usage-requests", headers=vn)).json()["items"]
    assert [(u["env"], u["status"]) for u in listed] == [("staging", "pending")]

    pending = (await cp.get(f"{V}/approvals", headers=vn)).json()["items"]
    assert [(p["kind"], p["subject"]) for p in pending] == [("usage", "mes.query_work_orders（staging）")]
    assert (await cp.get(f"{V}/approvals", headers=kh)).json()["items"] == []


async def test_audit_search_scoping_and_verify(
    cp: httpx.AsyncClient, db: Database, seeded: Seed, tmp_path: pathlib.Path
) -> None:
    spool = AuditSpool(tmp_path, "cp-test")
    now = dt.datetime.now(dt.UTC)
    for i, (kind, company) in enumerate(
        [("tool.invoke", seeded.vn), ("tool.invoke", seeded.kh), ("grant.create", seeded.vn)]
    ):
        spool.append(
            kind,
            company_id=company,
            now=now + dt.timedelta(seconds=i),
            record={"trace_id": f"t-{i}", "decision": "allow"},
        )
    assert await ship(spool, db.engine) == 3
    async with db.tx(DbScope.system()) as conn:
        await anchor(conn, platform_key())

    auditor_like = await login(cp, seeded, "admin")
    all_rows = (await cp.get(f"{V}/audit-logs", headers=auditor_like)).json()["items"]
    assert {r["record"]["trace_id"] for r in all_rows} == {"t-0", "t-1", "t-2"}

    q = (
        await cp.get(f"{V}/audit-logs", headers=auditor_like, params={"kind": "tool.invoke", "trace_id": "t-1"})
    ).json()
    assert [r["company_id"] for r in q["items"]] == [str(seeded.kh)]
    later = (now + dt.timedelta(seconds=1, milliseconds=500)).isoformat()
    assert [
        r["record"]["trace_id"]
        for r in (await cp.get(f"{V}/audit-logs", headers=auditor_like, params={"since": later})).json()["items"]
    ] == ["t-2"]
    limited = (await cp.get(f"{V}/audit-logs", headers=auditor_like, params={"limit": 2})).json()
    assert len(limited["items"]) == 2 and limited["has_more"] is True

    vn = await login(cp, seeded, "vn_admin")
    vn_rows = (await cp.get(f"{V}/audit-logs", headers=vn)).json()["items"]
    assert {r["company_id"] for r in vn_rows} == {str(seeded.vn)} and len(vn_rows) == 2

    data_owner = await login(cp, seeded, "security")
    assert (await cp.get(f"{V}/audit-logs", headers=data_owner)).status_code == 403

    assert (await cp.post(f"{V}/audit-logs/verify", headers=vn)).status_code == 403
    v = (await cp.post(f"{V}/audit-logs/verify", headers=auditor_like)).json()
    assert v == {"ok": True, "problems": [], "shards": 1, "records": 3}


async def test_console_static_mount(seeded: Seed, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>console</title>", encoding="utf-8")
    monkeypatch.setattr(cp_module, "CONSOLE_DIST", dist)
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as c:
        r = await c.get("/console/")
        assert r.status_code == 200 and "console" in r.text
        assert (await c.get("/healthz")).status_code == 200
