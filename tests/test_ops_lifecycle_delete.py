"""运营缺口：版本弃用/退役、系统连接删除、Connector 实例删除（architecture.md 7.4）。"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import text, update

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.conn import service as conn_svc
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as t
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from iam_seed import PASSWORD, Seed, actor_for, app_url
from m2_fixtures import publish_tool, tool_spec

pytestmark = pytest.mark.infra
SYS = Actor.system_actor()
V = "/api/v1"


@pytest.fixture
async def cp() -> httpx.AsyncClient:
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client


async def _login(cp: httpx.AsyncClient, seed: Seed, name: str = "admin") -> dict[str, str]:
    r = await cp.post(f"{V}/auth/login", json={"login_name": name, "password": PASSWORD, "totp": seed.code(name)})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


async def test_deprecate_then_retire_without_recent_use(db: Database, seeded: Seed) -> None:
    pub = await publish_tool(db, seeded, name="ops.retire_idle")
    async with db.tx(DbScope.system()) as conn:
        state = await registry.deprecate_version(conn, SYS, pub.version_id, reason="替代版本已上架")
        assert state == "deprecated"
        state = await registry.retire_version(conn, SYS, pub.version_id, reason="调用量归零，正式下线")
        assert state == "retired"
        row = await registry.version_row(conn, pub.version_id)
        assert row["state"] == "retired"


async def test_retire_blocked_when_recently_used_unless_force(db: Database, seeded: Seed) -> None:
    pub = await publish_tool(db, seeded, name="ops.retire_hot")
    async with db.tx(DbScope.system()) as conn:
        await registry.deprecate_version(conn, SYS, pub.version_id, reason="公告弃用")
        await conn.execute(
            update(t.grant).where(t.grant.c.id == pub.grant_vn).values(last_used_at=utcnow() - dt.timedelta(hours=1))
        )
        with pytest.raises(PlatformError) as e:
            await registry.retire_version(conn, SYS, pub.version_id, reason="想强行下线但未 force")
        assert e.value.code == ErrorCode.PRECONDITION_FAILED
        state = await registry.retire_version(
            conn, SYS, pub.version_id, reason="总部强制下线：业务下线", force=True
        )
        assert state == "retired"


async def test_deprecate_rejects_non_active(db: Database, seeded: Seed) -> None:
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, SYS, type_="tool", name="ops.dep_draft", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(conn, SYS, aid, "1.0.0", tool_spec())
        with pytest.raises(PlatformError) as e:
            await registry.deprecate_version(conn, SYS, vid, reason="误操作")
        assert e.value.code == ErrorCode.PRECONDITION_FAILED


async def test_delete_connection_and_connector_instance(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_svc.create_connection(
            conn,
            dev,
            company_id=seeded.hq,
            data={
                "system_key": "ops_del_sys",
                "env": "test",
                "base_url": "https://example.test",
                "scheme": "https",
                "tls_mode": "system_ca",
            },
        )
        iid = await conn_svc.create_connector_instance(
            conn, dev, name="ops_del_inst", kind="declarative", connection_id=cid, config={"operations": []}
        )
        with pytest.raises(PlatformError) as blocked:
            await conn_svc.delete_connection(conn, dev, cid)
        assert blocked.value.code == ErrorCode.PRECONDITION_FAILED

        await conn_svc.delete_connector_instance(conn, dev, iid)
        gone = (
            await conn.execute(text("SELECT id FROM conn.connector_instance WHERE id = :i"), {"i": iid})
        ).first()
        assert gone is None

        await conn_svc.delete_connection(conn, dev, cid)
        gone_c = (
            await conn.execute(text("SELECT id FROM conn.system_connection WHERE id = :c"), {"c": cid})
        ).first()
        assert gone_c is None


async def test_http_deprecate_retire_and_delete_routes(
    cp: httpx.AsyncClient, db: Database, seeded: Seed
) -> None:
    """经 HTTP：弃用→退役；删除连接器与连接；草稿 DELETE。"""
    admin = await _login(cp, seeded)
    pub = await publish_tool(db, seeded, name="ops.http_retire")

    dep = await cp.post(
        f"{V}/versions/{pub.version_id}/deprecate",
        headers=admin,
        json={"reason": "HTTP 测试：公告弃用"},
    )
    assert dep.status_code == 200, dep.text
    assert dep.json()["state"] == "deprecated"

    ret = await cp.post(
        f"{V}/versions/{pub.version_id}/retire",
        headers=admin,
        json={"reason": "HTTP 测试：调用量归零"},
    )
    assert ret.status_code == 200, ret.text
    assert ret.json()["state"] == "retired"

    # 草稿资产删除
    created = await cp.post(
        f"{V}/assets",
        headers=admin,
        json={"type": "tool", "name": "ops.http_draft_asset", "owner_org_id": str(seeded.hq), "classification": 2},
    )
    assert created.status_code == 201, created.text
    aid = created.json()["id"]
    ver = await cp.post(
        f"{V}/assets/{aid}/versions",
        headers=admin,
        json={"semver": "0.0.1", "spec": tool_spec()},
    )
    assert ver.status_code == 201, ver.text
    vid = ver.json()["id"]
    assert (await cp.delete(f"{V}/versions/{vid}", headers=admin)).status_code == 204
    assert (await cp.delete(f"{V}/assets/{aid}", headers=admin)).status_code == 204

    # 连接与实例
    conn_r = await cp.post(
        f"{V}/connections",
        headers=admin,
        json={
            "company_id": str(seeded.hq),
            "system_key": "ops_http_conn",
            "env": "test",
            "base_url": "https://ops-http.example.test",
            "scheme": "https",
            "tls_mode": "system_ca",
        },
    )
    assert conn_r.status_code == 201, conn_r.text
    cid = conn_r.json()["id"]
    inst = await cp.post(
        f"{V}/connector-instances",
        headers=admin,
        json={
            "name": "ops_http_inst",
            "kind": "declarative",
            "system_connection_id": cid,
            "config": {"operations": []},
        },
    )
    assert inst.status_code == 201, inst.text
    iid = inst.json()["id"]
    assert (await cp.delete(f"{V}/connections/{cid}", headers=admin)).status_code == 412
    assert (await cp.delete(f"{V}/connector-instances/{iid}", headers=admin)).status_code == 204
    assert (await cp.delete(f"{V}/connections/{cid}", headers=admin)).status_code == 204


async def test_upstream_mcp_source_create_disable_delete_blocks_connection(
    cp: httpx.AsyncClient, db: Database, seeded: Seed
) -> None:
    """上游 MCP 源登记 / 禁用 / 删除；绑定期间不可删系统连接。"""
    admin = await _login(cp, seeded)
    conn_r = await cp.post(
        f"{V}/connections",
        headers=admin,
        json={
            "company_id": str(seeded.hq),
            "system_key": "ops_up_mcp",
            "env": "test",
            "base_url": "https://upstream-mcp.example.test",
            "scheme": "https",
            "tls_mode": "system_ca",
        },
    )
    assert conn_r.status_code == 201, conn_r.text
    cid = conn_r.json()["id"]
    digest = "a" * 64
    created = await cp.post(
        f"{V}/upstream-mcp-sources",
        headers=admin,
        json={
            "name": "ops_up_src",
            "system_connection_id": cid,
            "tools_manifest_hash": digest,
            "mcp_path": "/mcp",
            "pinned_tools": [{"name": "ping"}],
        },
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert created.json()["status"] == "active"
    assert created.json()["tools_manifest_hash"] == digest

    listed = await cp.get(f"{V}/upstream-mcp-sources", headers=admin)
    assert listed.status_code == 200, listed.text
    assert any(x["id"] == sid for x in listed.json()["items"])

    assert (await cp.delete(f"{V}/connections/{cid}", headers=admin)).status_code == 412

    dis = await cp.post(f"{V}/upstream-mcp-sources/{sid}/disable", headers=admin)
    assert dis.status_code == 204, dis.text
    listed2 = await cp.get(f"{V}/upstream-mcp-sources", headers=admin)
    hit = next(x for x in listed2.json()["items"] if x["id"] == sid)
    assert hit["status"] == "disabled"

    # 禁用后仍占用连接，须删除源
    assert (await cp.delete(f"{V}/connections/{cid}", headers=admin)).status_code == 412
    assert (await cp.delete(f"{V}/upstream-mcp-sources/{sid}", headers=admin)).status_code == 204
    assert (await cp.delete(f"{V}/connections/{cid}", headers=admin)).status_code == 204
