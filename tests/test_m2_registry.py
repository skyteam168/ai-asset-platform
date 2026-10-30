"""M2 / P2：资产版本不可变、状态机、发布与回滚、发布快照与“控制面宕机仍可解析”。"""

from __future__ import annotations

import json
import pathlib

import asyncpg
import httpx
import pytest
from sqlalchemy import func, select, update

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as t
from aip.data_plane.app import create_app as create_dp
from aip.data_plane.app import pull_once
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.replay import MemoryReplayStore
from iam_seed import Seed, app_url, owner_dsn
from m2_fixtures import KEY, promote, publish_tool, snapshots, tool_spec

pytestmark = pytest.mark.infra
SYS = Actor.system_actor()


async def _draft(db: Database, s: Seed, name: str = "mes.tool") -> tuple[str, str]:
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(conn, SYS, type_="tool", name=name, owner_org_id=s.hq, classification=2)
        vid = await registry.create_version(conn, SYS, aid, "1.0.0", tool_spec())
    return str(aid), str(vid)


# ---------- 版本不可变 ----------


async def test_published_version_is_immutable_at_db_level(db: Database, seeded: Seed) -> None:
    """验收：已发布版本改动被触发器拒绝（即使绕过应用层、用 owner 账号直连数据库）。"""
    _, vid = await _draft(db, seeded)
    conn = await asyncpg.connect(owner_dsn())
    try:
        await conn.execute('UPDATE registry.asset_version SET spec = spec || \'{"name":"x"}\' WHERE id = $1', vid)
        await conn.execute("UPDATE registry.asset_version SET state = 'testing' WHERE id = $1", vid)
        for sql in (
            "UPDATE registry.asset_version SET spec = '{}' WHERE id = $1",
            "UPDATE registry.asset_version SET content_hash = 'sha256:x' WHERE id = $1",
            "UPDATE registry.asset_version SET semver = '9.9.9' WHERE id = $1",
            "DELETE FROM registry.asset_version WHERE id = $1",
        ):
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await conn.execute(sql, vid)
    finally:
        await conn.close()


async def test_published_version_is_immutable_at_app_level(db: Database, seeded: Seed) -> None:
    _, vid = await _draft(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        before = await registry.version_row(conn, vid)  # type: ignore[arg-type]
        await registry.update_draft(
            conn, SYS, before["id"], tool_spec(description="草稿阶段可以修改描述内容，哈希随之变化。")
        )
        after = await registry.version_row(conn, before["id"])
        assert after["content_hash"] != before["content_hash"]
        await registry.transition(conn, SYS, before["id"], "testing", "submit")
        with pytest.raises(PlatformError) as e:
            await registry.update_draft(conn, SYS, before["id"], tool_spec())
        assert e.value.code == ErrorCode.PRECONDITION_FAILED
        with pytest.raises(PlatformError, match="不可覆盖"):
            await registry.create_version(conn, SYS, before["asset_id"], "1.0.0", tool_spec())


async def test_discard_draft_version_and_delete_asset(db: Database, seeded: Seed) -> None:
    """草稿可删；离开 draft 后拒绝；无发布/授权的资产可整删。"""
    _aid, vid = await _draft(db, seeded, name="mes.discard_me")
    async with db.tx(DbScope.system()) as conn:
        await registry.delete_draft_version(conn, SYS, vid)  # type: ignore[arg-type]
        with pytest.raises(PlatformError) as missing:
            await registry.version_row(conn, vid)  # type: ignore[arg-type]
        assert missing.value.code == ErrorCode.NOT_FOUND

    aid2, vid2 = await _draft(db, seeded, name="mes.keep_shell")
    async with db.tx(DbScope.system()) as conn:
        await registry.transition(conn, SYS, vid2, "testing", "submit")  # type: ignore[arg-type]
        with pytest.raises(PlatformError) as e:
            await registry.delete_draft_version(conn, SYS, vid2)  # type: ignore[arg-type]
        assert e.value.code == ErrorCode.PRECONDITION_FAILED

    async with db.tx(DbScope.system()) as conn:
        empty = await registry.create_asset(
            conn, SYS, type_="tool", name="mes.empty_asset", owner_org_id=seeded.hq, classification=2
        )
        await registry.delete_asset(conn, SYS, empty)
        with pytest.raises(PlatformError) as gone:
            await registry.asset_row(conn, empty)
        assert gone.value.code == ErrorCode.NOT_FOUND
        with pytest.raises(PlatformError) as blocked:
            await registry.delete_asset(conn, SYS, aid2)  # type: ignore[arg-type]
        assert blocked.value.code == ErrorCode.PRECONDITION_FAILED
        # 删掉非草稿后才能清资产：先退回 draft 再删版本与资产
        await registry.transition(conn, SYS, vid2, "draft", "back")  # type: ignore[arg-type]
        await registry.delete_asset(conn, SYS, aid2)  # type: ignore[arg-type]


async def test_spec_validation_and_classification_consistency(db: Database, seeded: Seed) -> None:
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, SYS, type_="tool", name="erp.x", owner_org_id=seeded.hq, classification=3
        )
        with pytest.raises(PlatformError) as e:
            await registry.create_version(conn, SYS, aid, "1.0.0", {**tool_spec(), "risk_level": "write"})
        assert e.value.code == ErrorCode.INVALID_ARGUMENT and e.value.errors
        with pytest.raises(PlatformError, match="data_classification"):
            await registry.create_version(conn, SYS, aid, "1.0.0", tool_spec())
        with pytest.raises(PlatformError, match="语义化"):
            await registry.create_version(conn, SYS, aid, "v1", tool_spec(data_classification=3))


# ---------- 状态机 ----------


async def test_state_machine_legal_and_illegal_transitions(db: Database, seeded: Seed) -> None:
    _, vid = await _draft(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        v = (await registry.version_row(conn, vid))["id"]  # type: ignore[arg-type]
        with pytest.raises(PlatformError, match="非法状态迁移"):
            await registry.transition(conn, SYS, v, "active", "skip gates")
        await promote(conn, SYS, v, "active")
        await registry.transition(conn, SYS, v, "suspended", "risk rule")
        with pytest.raises(PlatformError, match="非法状态迁移"):
            await registry.transition(conn, SYS, v, "canary", "wrong return")
        await registry.transition(conn, SYS, v, "active", "reviewed")
        log = (
            await conn.execute(
                select(func.count()).select_from(t.state_transition).where(t.state_transition.c.version_id == v)
            )
        ).scalar_one()
    assert log == 7


async def test_suspended_version_snapshot_marks_release_unavailable(db: Database, seeded: Seed) -> None:
    pub = await publish_tool(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        await registry.transition(conn, SYS, pub.version_id, "suspended", "breaker")
    _, rel = await snapshots(db)
    assert rel["payload"]["releases"][0]["status"] == "suspended"


# ---------- 发布与回滚 ----------


async def test_release_supersede_and_rollback(db: Database, seeded: Seed) -> None:
    pub = await publish_tool(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        v2 = await registry.create_version(
            conn,
            SYS,
            pub.asset_id,
            "1.1.0",
            tool_spec(description="新版本：增加按车间过滤的可选参数，返回字段保持不变。"),
        )
        with pytest.raises(PlatformError, match="不能发布"):
            await registry.create_release(conn, SYS, v2, env="test", channel="stable", scope={"group": True})
        await promote(conn, SYS, v2, "approved")
        r2 = await registry.create_release(conn, SYS, v2, env="test", channel="stable", scope={"group": True})
        statuses = dict((await conn.execute(select(t.release.c.id, t.release.c.status))).all())
        assert statuses[pub.release_id] == "superseded" and statuses[r2] == "active"
        restored = await registry.rollback(conn, SYS, r2)
        statuses = dict((await conn.execute(select(t.release.c.id, t.release.c.status))).all())
    assert restored == pub.release_id
    assert statuses[pub.release_id] == "active" and statuses[r2] == "rolled_back"
    _, rel = await snapshots(db)
    assert [r["version"] for r in rel["payload"]["releases"]] == ["1.0.0"]


async def test_danger_tool_cannot_be_exposed_over_mcp(db: Database, seeded: Seed) -> None:
    spec = tool_spec(
        risk_level="danger",
        confirm_policy="approval",
        idempotency={"idempotent": True},
        recovery="manual",
        reconcile={"operation": "q", "by": "business_no"},
    )
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, SYS, type_="tool", name="erp.delete_po", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(conn, SYS, aid, "1.0.0", spec)
        await promote(conn, SYS, vid, "approved")
        with pytest.raises(PlatformError, match="danger"):
            await registry.create_release(
                conn, SYS, vid, env="test", channel="stable", scope={"group": True}, exposure=["rest", "mcp"]
            )


async def test_only_developers_create_assets(db: Database, seeded: Seed) -> None:
    from iam_seed import actor_for

    vn = await actor_for(db, seeded, "vn_admin")
    async with db.tx(vn.db_scope()) as conn:
        with pytest.raises(PlatformError) as e:
            await registry.create_asset(conn, vn, type_="tool", name="x.y", owner_org_id=seeded.vn, classification=1)
    assert e.value.code == ErrorCode.PERMISSION_DENIED


# ---------- 发布快照与“控制面宕机仍可服务” ----------


async def test_data_plane_resolves_tools_after_control_plane_goes_down(
    db: Database, seeded: Seed, tmp_path: pathlib.Path
) -> None:
    """验收：停掉 control_plane，数据面仍能按快照解析 Tool（含进程重启后从本地磁盘恢复）。"""
    await publish_tool(db, seeded)
    from aip.libs.tokens import keys

    keys.platform_keys.cache_clear()
    cp_app = create_cp(db=Database(app_url()))
    cp = httpx.AsyncClient(transport=httpx.ASGITransport(app=cp_app), base_url="https://cp")
    key = keys.platform_key()
    dp1 = create_dp(key=key, replay=MemoryReplayStore(), data_dir=tmp_path)
    token = get_settings().internal_service_token
    assert await pull_once(cp, dp1.state.iam, token)
    assert await pull_once(cp, dp1.state.releases, token)
    assert (tmp_path / "snapshots" / "release.json").exists()

    # 控制面“宕机”：新的数据面进程只能连到一个不存在的控制面地址
    dp2 = create_dp(key=key, replay=MemoryReplayStore(), data_dir=tmp_path, control_plane_url="http://127.0.0.1:9")
    async with dp2.router.lifespan_context(dp2):
        entry = dp2.state.releases.resolve("production.mes.query_work_orders", "test")
        assert entry["version"] == "1.0.0" and entry["spec"]["risk_level"] == "read"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dp2), base_url="https://dp") as c:
            ready = await c.get("/readyz")
        assert ready.status_code == 200, ready.json()


async def test_tampered_local_snapshot_is_not_restored(db: Database, seeded: Seed, tmp_path: pathlib.Path) -> None:
    await publish_tool(db, seeded)
    _, rel = await snapshots(db)
    dp = create_dp(key=KEY, replay=MemoryReplayStore(), data_dir=tmp_path)
    dp.state.releases.load(rel)
    path = tmp_path / "snapshots" / "release.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["payload"]["releases"][0]["spec"]["risk_level"] = "danger"
    path.write_text(json.dumps(doc), encoding="utf-8")
    fresh = create_dp(key=KEY, replay=MemoryReplayStore(), data_dir=tmp_path)
    assert fresh.state.releases.restore_from_disk() is False
    with pytest.raises(PlatformError) as e:
        fresh.state.releases.resolve("production.mes.query_work_orders", "test")
    assert e.value.code == ErrorCode.POLICY_UNAVAILABLE


async def test_release_snapshot_pins_exact_version(db: Database, seeded: Seed) -> None:
    await publish_tool(db, seeded)
    _, rel = await snapshots(db)
    dp = create_dp(key=KEY, replay=MemoryReplayStore(), data_dir=None)
    dp.state.releases.load(rel, persist=False)
    assert dp.state.releases.resolve("production.mes.query_work_orders", "test", version="1.0.0")
    with pytest.raises(PlatformError) as e:
        dp.state.releases.resolve("production.mes.query_work_orders", "test", version="1.1.0")
    assert e.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(PlatformError):
        dp.state.releases.resolve("production.mes.query_work_orders", "prod")
    async with db.tx(DbScope.system()) as conn:
        await conn.execute(update(t.release).values(status="superseded"))
