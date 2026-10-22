"""H38：Swagger 2.0 文档经升级后走 OpenAPI Importer。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.importer import openapi as oa
from aip.control_plane.importer.service import import_openapi
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import Seed, actor_for

ROOT = Path(__file__).resolve().parents[1]
SWAGGER2 = (ROOT / "tests" / "fixtures" / "swagger2-pet-mini.yaml").read_text(encoding="utf-8")
MES = (ROOT / "tests" / "contracts" / "mes-vn01.openapi.yaml").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_preview_swagger2_upgrades(db: Database, seeded: Seed) -> None:
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        out = await import_openapi(
            conn,
            dev,
            document=SWAGGER2,
            connector_name="pet_sw2",
            domain="demo",
            system="pet",
            persist=False,
        )
    ops = {o["operation"] for o in out["connector_config"]["operations"]}
    assert "list_pets" in ops
    assert "create_pet" in ops
    assert len(out["tools"]) >= 2


def test_swagger_1_rejected() -> None:
    with pytest.raises(PlatformError) as ei:
        oa.load_document('swagger: "1.2"\ninfo: {title: x, version: "1"}\npaths: {}')
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


def test_openapi3_unchanged() -> None:
    doc = oa.load_document(MES)
    assert str(doc.get("openapi", "")).startswith("3.")
    ops, _ = oa.parse_operations(doc)
    assert any(o["operation"] == "list_work_orders" for o in ops)
