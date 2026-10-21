"""H9：G0 PII 辅助发现（规则；Presidio 可选）。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from aip.control_plane.conn import service as conn_service
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.libs.pii.suggest import suggest_pii_fields
from iam_seed import Seed
from m2_fixtures import tool_spec
from test_m4_gates import _people


def test_suggest_from_field_names() -> None:
    spec = tool_spec(
        input_schema={
            "type": "object",
            "properties": {
                "factory_id": {"type": "string"},
                "worker_phone": {"type": "string", "description": "联系电话"},
                "email": {"type": "string"},
            },
        }
    )
    sug = suggest_pii_fields(spec)
    fields = {s["field"] for s in sug}
    assert "worker_phone" in fields and "email" in fields
    assert "factory_id" not in fields


def test_suggest_skips_declared() -> None:
    spec = tool_spec(
        input_schema={"type": "object", "properties": {"email": {"type": "string"}}},
        pii_fields=["email"],
    )
    assert suggest_pii_fields(spec) == []


@pytest.mark.asyncio
async def test_g0_returns_pii_suggestions(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = f"ops.h9.pii_{uuid.uuid4().hex[:6]}"
    async with db.tx(DbScope.system()) as conn:
        if (
            await conn.execute(
                select(conn_service.connector_instance.c.id).where(conn_service.connector_instance.c.name == "mes_mock")
            )
        ).first() is None:
            await conn_service.create_connector_instance(
                conn,
                p.admin,
                name="mes_mock",
                kind="mock",
                connection_id=None,
                config={"operations": {"q": {"response": []}}},
            )
        aid = await registry.create_asset(conn, p.dev, type_="tool", name=cap, owner_org_id=seeded.hq, classification=2)
        spec = tool_spec(
            capability_id=cap,
            name=cap.split(".")[-1],
            connector_ref={"connector": "mes_mock", "operation": "q"},
            input_schema={
                "type": "object",
                "properties": {
                    "factory_id": {"type": "string"},
                    "mobile": {"type": "string"},
                },
                "required": ["factory_id"],
            },
        )
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)
        await gates.add_test_case(
            conn, p.dev, vid, name="f1", kind="functional", args={"factory_id": "F1"}, expect={"status": 200}
        )
        await gates.add_test_case(
            conn,
            p.dev,
            vid,
            name="p1",
            kind="permission",
            args={"factory_id": "F1"},
            expect={"denied": True},
            principal={"clearance": 1},
        )
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["pii"]})
        g0 = await gates.submit(conn, p.dev, vid)
    assert g0["passed"] is True
    assert any(s["field"] == "mobile" for s in g0.get("pii_suggestions") or [])
