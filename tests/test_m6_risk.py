"""M6 / P9：风险规则、自动 Suspended、复核恢复（architecture.md 12.4）。

验收：触发规则后自动熔断并告警，恢复需复核。
审计事件用真实 spool 写入并入库（与数据面相同的路径），规则扫描读审计库与领域事件。
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import users
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import snapshot as release_snapshot
from aip.control_plane.risk import service as risk
from aip.libs.audit.spool import AuditSpool
from aip.libs.audit.store import ship
from aip.libs.common.errors import PlatformError
from aip.libs.tokens.secrets_util import unseal
from iam_seed import PASSWORD, Seed, app_url
from m2_fixtures import Published, publish_tool
from test_m1_api import login

pytestmark = pytest.mark.infra
CAP = "production.mes.query_work_orders"
SYS = Actor.system_actor()
NOW = dt.datetime(2026, 10, 20, 2, 0, tzinfo=dt.UTC)  # 北京时间 10:00，周二


async def _emit(
    db: Database, tmp: pathlib.Path, kind: str, records: list[dict[str, Any]], company: Any, at: dt.datetime
) -> None:
    # 每次用独立实例 id：同一 shard 重新从 seq 1 写会被入库端当作重复投递忽略
    instance = f"dp-test-{uuid.uuid4().hex[:6]}"
    spool = AuditSpool(tmp / instance, instance)
    for i, rec in enumerate(records):
        spool.append(kind, company_id=company, record=rec, now=at + dt.timedelta(seconds=i))
    assert await ship(spool, db.engine) == len(records)


async def _tune(db: Database, code: str, **params: Any) -> None:
    async with db.tx(DbScope.system()) as conn:
        await risk.update_rule(conn, SYS, code, {"params": params})


async def _scan(db: Database, now: dt.datetime) -> list[dict[str, Any]]:
    async with db.tx(DbScope.system()) as conn:
        return await risk.scan(conn, now)


async def _state(db: Database, vid: uuid.UUID) -> str:
    async with db.tx(DbScope.system()) as conn:
        return str((await registry.version_row(conn, vid))["state"])


async def _second_owner(db: Database, s: Seed) -> Actor:
    async with db.tx(DbScope.system()) as conn:
        c = await users.create_user(
            conn, SYS, display_name="sec2", home_org_id=s.hq, login_name="sec2", password=PASSWORD, login_enabled=True
        )
        await users.assign_role(conn, SYS, c.id, "data_owner", s.group)
        s.users["sec2"] = c.id
        s.totp["sec2"] = unseal((await users.user_row(conn, c.id))["totp_secret_enc"])
        return await users.load_actor(conn, c.id)


def _default_rules() -> list[tuple[Any, ...]]:
    """规则的默认值以迁移为准（测试之间恢复，避免调参互相影响）。"""
    import importlib.util

    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20261020_0008_risk.py"
    spec = importlib.util.spec_from_file_location("risk_migration", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.RULES)


@pytest.fixture(autouse=True)
async def reset_rules(db: Database) -> None:
    async with db.tx(DbScope.system()) as conn:
        for code, _, _, _, params, action, severity in _default_rules():
            await conn.execute(
                text(
                    "UPDATE risk.rule SET params = CAST(:p AS jsonb), action = :a, severity = :s, enabled = true "
                    "WHERE code = :c"
                ),
                {"c": code, "p": json.dumps(params, ensure_ascii=False), "a": action, "s": severity},
            )


@pytest.fixture
async def published(db: Database, seeded: Seed) -> Published:
    return await publish_tool(db, seeded)


def _writes(consumer: str, n: int, **over: Any) -> list[dict[str, Any]]:
    return [
        {"capability_id": CAP, "consumer_id": consumer, "status": "succeeded", "write": True, **over} for _ in range(n)
    ]


async def test_bulk_write_suspends_and_resume_needs_review(
    db: Database, seeded: Seed, published: Published, tmp_path: pathlib.Path
) -> None:
    notified: list[dict[str, Any]] = []

    async def capture(event: dict[str, Any]) -> None:
        notified.append(event)

    risk.register_notifier(capture)
    try:
        await _tune(db, "bulk_write", threshold=5, window_s=300)
        consumer = str(seeded.consumers["vn-ai"])
        await _emit(db, tmp_path, "tool.execution", _writes(consumer, 6), seeded.vn, NOW - dt.timedelta(seconds=60))
        await _emit(
            db, tmp_path, "tool.execution", _writes(consumer, 3, write=False), seeded.vn, NOW - dt.timedelta(seconds=30)
        )
        created = await _scan(db, NOW)
    finally:
        risk._notifiers.remove(capture)
    bulk = [e for e in created if e["rule_code"] == "bulk_write"]
    assert len(bulk) == 1 and bulk[0]["observed"] == 6
    assert bulk[0]["action_result"]["suspended"][0]["from"] == "active"
    assert await _state(db, published.version_id) == "suspended"
    assert [e["rule_code"] for e in notified] == [e["rule_code"] for e in created]  # 告警
    async with db.tx(DbScope.system()) as conn:
        payload = await release_snapshot.build_payload(conn)
    assert {r["capability_id"]: r["status"] for r in payload["releases"]}[CAP] == "suspended"  # PDP 随快照拒绝
    assert [e["rule_code"] for e in await _scan(db, NOW + dt.timedelta(seconds=5))] == []  # 同一窗口不重复处置

    # 恢复：数据 Owner 写明修复情况发起复核；本人不能批准，另一位数据 Owner 批准后回到 active
    event_id = uuid.UUID(bulk[0]["id"])
    async with db.tx(DbScope.system()) as conn:
        sec = await users.load_actor(conn, seeded.users["security"])
        with pytest.raises(PlatformError, match="修复情况"):
            await risk.request_resume(conn, sec, event_id, "")
        req = await risk.request_resume(conn, sec, event_id, "已限制该消费方的批量写入频率")
    sec2 = await _second_owner(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError):
            await risk.decide_resume(conn, sec, uuid.UUID(req["approval_id"]), True, "自己批")
    async with db.tx(DbScope.system()) as conn:
        assert await risk.decide_resume(conn, sec2, uuid.UUID(req["approval_id"]), True, "复核通过") == "approved"
    assert await _state(db, published.version_id) == "active"
    async with db.tx(DbScope.system()) as conn:
        [ev] = [e for e in await risk.list_events(conn) if e["id"] == bulk[0]["id"]]
    assert ev["status"] == "resolved"


async def test_error_ratio_and_alert_rules(
    db: Database, seeded: Seed, published: Published, tmp_path: pathlib.Path
) -> None:
    await _tune(db, "error_spike", min_count=5, threshold=0.5)
    await _tune(db, "denied_attempts", threshold=4)
    await _tune(db, "clearance_probe", threshold=3)
    await _tune(db, "assertion_failures", threshold=3)
    recs = [
        {"capability_id": CAP, "status": s} for s in ["failed", "unknown", "failed", "succeeded", "failed", "succeeded"]
    ]
    await _emit(db, tmp_path, "tool.execution", recs, seeded.vn, NOW - dt.timedelta(seconds=100))
    deny = [{"client_id": "vn-ai", "end_user_id": "u1", "decision": "deny", "reasons": ["CLASSIFICATION_DENIED"]}] * 4
    await _emit(db, tmp_path, "pdp.decision", deny, seeded.vn, NOW - dt.timedelta(seconds=100))
    await _emit(
        db,
        tmp_path,
        "auth.assertion_failed",
        [{"client_id": "kh-ai", "code": "ASSERTION_REPLAYED"}] * 3,
        seeded.kh,
        NOW - dt.timedelta(seconds=10),
    )
    async with db.tx(DbScope.system()) as conn:
        await conn.execute(
            text(
                "INSERT INTO registry.domain_event (company_id, kind, subject_id, payload, occurred_at) "
                "VALUES (:c, 'tls.fingerprint_changed', :s, '{}'::jsonb, :t)"
            ),
            {"c": seeded.vn, "s": uuid.uuid4(), "t": NOW - dt.timedelta(seconds=30)},
        )
    created = {e["rule_code"]: e for e in await _scan(db, NOW)}
    assert set(created) == {
        "error_spike",
        "denied_attempts",
        "clearance_probe",
        "assertion_failures",
        "fingerprint_changed",
    }
    assert created["error_spike"]["observed"] == pytest.approx(4 / 6, abs=1e-3)
    assert created["error_spike"]["action_result"]["suspended"]
    assert created["assertion_failures"]["company_id"] == str(seeded.kh)
    assert created["denied_attempts"]["action_result"] == {"alerted": True}
    async with db.tx(DbScope.system()) as conn:
        sec = await users.load_actor(conn, seeded.users["security"])
        done = await risk.acknowledge(conn, sec, uuid.UUID(created["denied_attempts"]["id"]), "已联系该部门核实")
        assert done["status"] == "resolved"
        kept = await risk.acknowledge(conn, sec, uuid.UUID(created["error_spike"]["id"]), "排查中")
        assert kept["status"] == "acknowledged"  # 熔断类不能直接关闭，只能走复核恢复


async def test_off_hours_uses_company_timezone(db: Database, seeded: Seed, tmp_path: pathlib.Path) -> None:
    await _tune(db, "off_hours_write", tz_by_company_code={"VN01": "Asia/Ho_Chi_Minh"}, default_tz="Asia/Shanghai")
    consumer = str(seeded.consumers["vn-ai"])
    night = dt.datetime(2026, 10, 20, 15, 30, tzinfo=dt.UTC)  # 越南 22:30
    day = dt.datetime(2026, 10, 20, 4, 0, tzinfo=dt.UTC)  # 越南 11:00
    await _emit(db, tmp_path, "tool.execution", _writes(consumer, 1), seeded.vn, night)
    await _emit(db, tmp_path, "tool.execution", _writes(str(seeded.consumers["kh-ai"]), 1), seeded.kh, day)
    hits = [e for e in await _scan(db, night + dt.timedelta(minutes=5)) if e["rule_code"] == "off_hours_write"]
    assert [h["group_key"]["consumer_id"] for h in hits] == [consumer]


async def test_rule_validation(db: Database, seeded: Seed) -> None:
    async with db.tx(DbScope.system()) as conn:
        vn_admin = await users.load_actor(conn, seeded.users["vn_admin"])
        with pytest.raises(PlatformError, match="平台管理员"):
            await risk.update_rule(conn, vn_admin, "call_burst", {"enabled": False})
        with pytest.raises(PlatformError, match="字段名"):
            await risk.update_rule(conn, SYS, "call_burst", {"params": {"group_by": ["x'; DROP TABLE risk.rule; --"]}})
        with pytest.raises(PlatformError, match="capability_id"):
            await risk.update_rule(conn, SYS, "denied_attempts", {"action": "suspend_asset"})
        with pytest.raises(Exception, match="Nowhere"):
            await risk.update_rule(
                conn, SYS, "off_hours_write", {"params": {"off_hours": {"default_tz": "Mars/Nowhere"}}}
            )


async def test_risk_api(db: Database, seeded: Seed, published: Published, tmp_path: pathlib.Path) -> None:
    app = create_cp(db=Database(app_url()))
    cp = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp")
    admin = await login(cp, seeded, "admin")
    sec = await login(cp, seeded, "security")
    vn = await login(cp, seeded, "vn_admin")
    rules = (await cp.get("/api/v1/risk-rules", headers=admin)).json()["items"]
    assert len(rules) == 8
    # 本用例按真实时间扫描：关掉非工作时段规则，免得结果取决于运行时刻
    assert (
        await cp.put("/api/v1/risk-rules/off_hours_write", json={"enabled": False}, headers=admin)
    ).status_code == 200
    r = await cp.put("/api/v1/risk-rules/bulk_write", json={"params": {"threshold": 2}}, headers=admin)
    assert r.status_code == 200 and r.json()["params"]["threshold"] == 2
    assert (await cp.put("/api/v1/risk-rules/bulk_write", json={"enabled": False}, headers=sec)).status_code == 403
    await _emit(
        db,
        tmp_path,
        "tool.execution",
        _writes(str(seeded.consumers["vn-ai"]), 3),
        seeded.vn,
        dt.datetime.now(dt.UTC) - dt.timedelta(seconds=20),
    )
    assert (await cp.post("/api/v1/risk-scan", headers=sec)).status_code == 403
    created = (await cp.post("/api/v1/risk-scan", headers=admin)).json()["created"]
    assert [e["rule_code"] for e in created] == ["bulk_write"]
    events = (await cp.get("/api/v1/risk-events", headers=sec, params={"status": "open"})).json()["items"]
    assert [e["rule_name"] for e in events] == ["批量写"]
    vn_events = (await cp.get("/api/v1/risk-events", headers=vn)).json()["items"]
    assert len(vn_events) == 1  # RLS：越南分公司看得到本公司事件
    kh = await login(cp, seeded, "kh_admin")
    assert (await cp.get("/api/v1/risk-events", headers=kh)).json()["items"] == []
    url = f"/api/v1/risk-events/{events[0]['id']}/request-resume"
    req = (await cp.post(url, json={"note": "已修复限流配置"}, headers=sec)).json()
    await _second_owner(db, seeded)
    sec2 = await login(cp, seeded, "sec2")
    pending = (await cp.get("/api/v1/approvals", headers=sec2)).json()["items"]
    mine = [p for p in pending if p["id"] == req["approval_id"]]
    assert mine and mine[0]["subject"].startswith("风险熔断恢复"), (req, pending)


async def test_data_plane_audits_assertion_failures(tmp_path: pathlib.Path) -> None:
    from m3_world import World, connector, tool
    from test_m5_workflow import STATIC

    w = World(
        tmp_path,
        [tool(CAP, "m", "q")],
        [connector("m", "mock", {"operations": {"q": {"response": []}}})],
        static_token=STATIC,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://dp") as c:
        r = await c.post(
            f"/api/v1/invoke/{CAP}",
            json={"args": {"factory_id": "F001"}},
            headers={"Authorization": f"Bearer {STATIC}", "X-User-Assertion": "not-a-jwt"},
        )
    assert r.status_code == 401
    lines = [json.loads(x) for f in (tmp_path / "audit-spool").glob("*.jsonl") for x in f.read_text().splitlines()]
    [rec] = [x for x in lines if x["kind"] == "auth.assertion_failed"]
    assert rec["record"] == {"client_id": "vn-ai", "code": "ASSERTION_INVALID"}
