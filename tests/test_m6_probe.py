"""M6d：合成探针与契约漂移。

验收：探针连续失败后版本带漂移标记并产生 G1 回归记录。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.probe import service as probe
from aip.control_plane.registry import service as registry
from aip.data_plane.probe_scheduler import ProbeScheduler, probe_principal
from iam_seed import Seed
from m2_fixtures import tool_spec
from m3_world import World, connector, tool
from test_m4_gates import SECRETS, People, _people

pytestmark = pytest.mark.infra


async def _publish_probed_tool(db: Database, s: Seed, p: People) -> tuple[uuid.UUID, str]:
    spec = tool_spec(
        probe={"args": {"factory_id": "VN01"}, "interval_s": 300},
        output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
    )
    async with db.tx(DbScope.system()) as conn:
        from m2_fixtures import promote

        aid = await registry.create_asset(
            conn, p.dev, type_="tool", name="mes.probe_demo", owner_org_id=s.hq, classification=2
        )
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["探针回归"]})
        await gates.add_test_case(
            conn,
            p.dev,
            vid,
            name="happy",
            kind="functional",
            args={"factory_id": "VN01"},
            expect={"status": "succeeded"},
        )
        await gates.add_test_case(
            conn,
            p.dev,
            vid,
            name="deny",
            kind="permission",
            args={"factory_id": "KH01"},
            expect={"status": "failed"},
            principal={"clearance": 1},
        )
        await promote(conn, p.dev, vid)
        await registry.create_release(
            conn, p.admin, vid, env="test", channel="stable", scope={"group": True}, exposure=["rest", "mcp"]
        )
    return vid, spec["capability_id"]


async def test_three_failures_mark_drift_and_g1(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid, cap = await _publish_probed_tool(db, seeded, p)

    async with db.tx(DbScope.system()) as conn:
        result: dict = {}
        for _ in range(3):
            result = await probe.record_probe(
                conn,
                version_id=vid,
                capability_id=cap,
                outcome="failed",
                schema_valid=False,
                latency_ms=12,
                detail={"status": "failed"},
                secrets=SECRETS,
            )
        assert result["drift"] is True
        drift = await probe.drift_row(conn, vid)
        assert drift is not None and drift["consecutive_failures"] >= 3
        hist = await gates.gate_history(conn, vid)
        assert any(h["gate"] == "G1_regression" for h in hist)


async def test_scheduler_schema_invalid(tmp_path: Path) -> None:
    """探针调度：下游返回不符合 output_schema → schema_invalid 并入队事件。"""
    spec = tool(
        "production.mes.query_work_orders",
        "mes",
        "query_work_orders",
        probe={"args": {"factory_id": "F001"}, "interval_s": 300},
        # mock 返回 array；此处要求 object → schema_invalid
        output_schema={"type": "object", "required": ["must_exist"], "properties": {"must_exist": {"type": "string"}}},
        scope_binding={},
    )
    world = World(
        tmp_path,
        [spec],
        [connector("mes", "mock", {"operations": {"query_work_orders": {"response": {"wo": 1}}}})],
    )
    await world.app.state.policy.sync_policy()
    sched = ProbeScheduler(world.app.state.runtime, world.app.state.releases, world.app.state.events)
    # 注入 version_id 供事件携带（快照可能无此字段）
    rel = world.app.state.releases.releases("test")[0]
    rel = {**rel, "version_id": str(uuid.uuid4())}
    out = await sched.run_one(rel)
    assert out["outcome"] == "schema_invalid"
    assert out["schema_valid"] is False
    assert any(e["kind"] == "probe.result" for e in world.app.state.events.queue)


def test_probe_principal_flag() -> None:
    p = probe_principal()
    assert p.client_id == "platform-probe" and p.extra.get("probe") is True
