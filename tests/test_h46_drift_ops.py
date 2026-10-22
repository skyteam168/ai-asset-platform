"""H46：契约漂移运维 API。"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from aip.control_plane.app import create_app
from aip.control_plane.db import Database, DbScope
from aip.control_plane.probe import service as probe
from aip.libs.common.ids import utcnow
from iam_seed import Seed, app_url
from m2_fixtures import publish_tool
from test_m1_api import login

pytestmark = pytest.mark.infra


@pytest.fixture
async def cp(db: Database) -> httpx.AsyncClient:
    app = create_app(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client


async def test_list_and_clear_drift(cp: httpx.AsyncClient, db: Database, seeded: Seed) -> None:
    pub = await publish_tool(db, seeded)
    version_id = pub.version_id
    async with db.tx(DbScope.system()) as conn:
        await conn.execute(
            text(
                """
                INSERT INTO registry.contract_drift
                  (version_id, company_id, capability_id, marked_at, reason, consecutive_failures)
                VALUES (:v, :c, :cap, :t, 'schema invalid', 3)
                ON CONFLICT (version_id) DO UPDATE SET
                  cleared_at = NULL, reason = EXCLUDED.reason, marked_at = EXCLUDED.marked_at
                """
            ),
            {"v": version_id, "c": seeded.vn, "cap": "mes.query_work_orders", "t": utcnow()},
        )
    admin = await login(cp, seeded, "admin")
    r = await cp.get("/api/v1/contract-drifts", headers=admin)
    assert r.status_code == 200, r.text
    ids = {str(i["version_id"]) for i in r.json()["items"]}
    assert str(version_id) in ids
    c = await cp.post(f"/api/v1/contract-drifts/{version_id}/clear", headers=admin)
    assert c.status_code == 200, c.text
    async with db.tx(DbScope.system()) as conn:
        row = await probe.drift_row(conn, version_id)
    assert row is None
