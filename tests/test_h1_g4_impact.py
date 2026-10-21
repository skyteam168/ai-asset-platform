"""H1：G4 依赖影响分析与上层回归（architecture.md 6.4 / 7.1）。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, update

from aip.connectors.credentials import MemorySecretStore
from aip.control_plane.conn import service as conn_service
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.gates.impact import list_impact
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as rt
from iam_seed import Seed
from m2_fixtures import tool_spec
from test_m4_gates import CASES, OUTPUT, _draft, _people, _state

pytestmark = pytest.mark.infra
SYS = Actor.system_actor()
SECRETS = MemorySecretStore()


async def _add_cases(conn: Any, actor: Actor, vid: uuid.UUID) -> None:
    for c in CASES:
        await gates.add_test_case(
            conn,
            actor,
            vid,
            name=c["name"],
            kind=c["kind"],
            args=c["args"],
            principal=c["principal"],
            expect=c["expect"],
        )


async def _ensure_mes(conn: Any, admin: Actor) -> None:
    if (
        await conn.execute(
            select(conn_service.connector_instance.c.id).where(conn_service.connector_instance.c.name == "mes_mock")
        )
    ).first() is None:
        await conn_service.create_connector_instance(
            conn,
            admin,
            name="mes_mock",
            kind="mock",
            connection_id=None,
            config={"operations": {"q": {"response": [{"wo_no": "W", "qty": 1, "status": "open"}]}}},
        )


async def test_tool_without_dependents_g4_empty(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _draft(db, seeded, p, name=f"mes.wo.{uuid.uuid4().hex[:8]}")
    async with db.tx(DbScope.system()) as conn:
        assert (await gates.submit(conn, p.dev, vid))["passed"]
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS)
        assert lab["passed"]
        assert lab["g4"]["impacted"] == []
        hist = await gates.gate_history(conn, vid)
        assert any(h["gate"] == "G4" and h["status"] == "passed" for h in hist)
        impact = await list_impact(conn, vid)
        assert impact == []
    assert await _state(db, vid) == "in_review"


async def test_bundle_impact_listed_and_g4_passes(db: Database, seeded: Seed) -> None:
    """Bundle 引用 tool@1.0.0；测 tool 2.0.0 时 impact 含 Bundle，G4 因 1.0.0 仍可发布而通过。"""
    p = await _people(db, seeded)
    cap = f"production.mes.query_{uuid.uuid4().hex[:8]}"
    async with db.tx(DbScope.system()) as conn:
        await _ensure_mes(conn, p.admin)
        aid = await registry.create_asset(
            conn, p.dev, type_="tool", name=cap, owner_org_id=seeded.hq, classification=2
        )
        spec = tool_spec(
            capability_id=cap,
            connector_ref={"connector": "mes_mock", "operation": "q"},
            output_schema=OUTPUT,
        )
        v1 = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)
        await _add_cases(conn, p.dev, v1)
        await gates.set_acceptance(conn, p.dev, v1, {"scenarios": ["x"]})
        assert (await gates.submit(conn, p.dev, v1))["passed"]
        assert (await gates.run_lab(conn, p.dev, v1, secrets=SECRETS))["passed"]
        # 跳过 G5/G6：直接标为 approved 以便 Bundle 组合门禁
        await conn.execute(update(rt.asset_version).where(rt.asset_version.c.id == v1).values(state="approved"))

        bname = f"pack_{uuid.uuid4().hex[:8]}"
        bid = await registry.create_asset(
            conn, p.dev, type_="bundle", name=bname, owner_org_id=seeded.hq, classification=2
        )
        bspec = {
            "name": bname,
            "description": "G4 影响分析测试套件，引用读类 Tool。",
            "tools": [f"{cap}@1.0.0"],
        }
        bvid = await registry.create_version(conn, p.dev, bid, "1.0.0", bspec)
        await conn.execute(update(rt.asset_version).where(rt.asset_version.c.id == bvid).values(state="active"))

        v2 = await registry.create_version(conn, p.dev, aid, "2.0.0", spec)
        await _add_cases(conn, p.dev, v2)
        await gates.set_acceptance(conn, p.dev, v2, {"scenarios": ["x"]})
        assert (await gates.submit(conn, p.dev, v2))["passed"]
        impact = await list_impact(conn, v2)
        assert any(i["version_id"] == str(bvid) for i in impact)
        lab = await gates.run_lab(conn, p.dev, v2, secrets=SECRETS)
        assert lab["passed"], lab
        assert any(i["version_id"] == str(bvid) for i in lab["g4"]["impacted"])
    assert await _state(db, v2) == "in_review"


async def test_g4_fails_when_parent_pins_unreleased_version(db: Database, seeded: Seed) -> None:
    """Bundle 已钉住 tool@2.0.0，但 2.0.0 尚在 testing → G4 失败回 draft。"""
    p = await _people(db, seeded)
    cap = f"production.mes.pin_{uuid.uuid4().hex[:8]}"
    async with db.tx(DbScope.system()) as conn:
        await _ensure_mes(conn, p.admin)
        aid = await registry.create_asset(
            conn, p.dev, type_="tool", name=cap, owner_org_id=seeded.hq, classification=2
        )
        spec = tool_spec(
            capability_id=cap,
            connector_ref={"connector": "mes_mock", "operation": "q"},
            output_schema=OUTPUT,
        )
        v2 = await registry.create_version(conn, p.dev, aid, "2.0.0", spec)
        await _add_cases(conn, p.dev, v2)
        await gates.set_acceptance(conn, p.dev, v2, {"scenarios": ["x"]})

        bname = f"pin_{uuid.uuid4().hex[:8]}"
        bid = await registry.create_asset(
            conn, p.dev, type_="bundle", name=bname, owner_org_id=seeded.hq, classification=2
        )
        bspec = {
            "name": bname,
            "description": "钉住未发布 Tool 版本的套件，用于 G4 失败用例。",
            "tools": [f"{cap}@2.0.0"],
        }
        bvid = await registry.create_version(conn, p.dev, bid, "1.0.0", bspec)
        await conn.execute(update(rt.asset_version).where(rt.asset_version.c.id == bvid).values(state="active"))

        assert (await gates.submit(conn, p.dev, v2))["passed"]
        lab = await gates.run_lab(conn, p.dev, v2, secrets=SECRETS)
        assert lab["passed"] is False
        assert lab["g4"]["failed"]
        assert any(f["version_id"] == str(bvid) for f in lab["g4"]["failed"])
    assert await _state(db, v2) == "draft"
