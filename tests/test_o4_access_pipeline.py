"""O4：运营概览接入管道（草稿→门禁→发布→Grant）。"""

from __future__ import annotations

import httpx
import pytest

from aip.control_plane.app import create_app
from aip.control_plane.db import Database, DbScope
from aip.control_plane.registry import service as registry
from iam_seed import Seed, actor_for, app_url
from m2_fixtures import tool_spec
from test_m1_api import login

pytestmark = pytest.mark.infra


@pytest.fixture
async def cp(db: Database) -> httpx.AsyncClient:
    app = create_app(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client


async def test_ops_summary_includes_pipeline(cp: httpx.AsyncClient, seeded: Seed, db: Database) -> None:
    admin = await login(cp, seeded, "admin")
    r = await cp.get("/api/v1/console/ops-summary", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    pipe = body["pipeline"]
    assert set(pipe["totals"]) == {"draft", "gating", "published", "granted"}
    assert isinstance(pipe["by_company"], list)
    assert isinstance(pipe["active_connections"], int)

    before = pipe["totals"]["draft"]
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, dev, type_="tool", name="o4.pipeline.draft_tool", owner_org_id=seeded.vn, classification=2
        )
        await registry.create_version(
            conn,
            dev,
            aid,
            "0.1.0",
            tool_spec(capability_id="o4.pipeline.draft_tool", connector_ref={"connector": "none", "operation": "q"}),
        )

    r2 = await cp.get("/api/v1/console/ops-summary", headers=admin)
    assert r2.status_code == 200, r2.text
    after = r2.json()["pipeline"]["totals"]["draft"]
    assert after >= before + 1
    companies = {c["company_id"]: c for c in r2.json()["pipeline"]["by_company"]}
    assert str(seeded.vn) in companies
    assert companies[str(seeded.vn)]["draft"] >= 1
