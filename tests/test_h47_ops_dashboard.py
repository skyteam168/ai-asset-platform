"""H47：运营概览 ops-summary。"""

from __future__ import annotations

import httpx
import pytest

from aip.control_plane.app import create_app
from aip.control_plane.db import Database
from iam_seed import Seed, app_url
from test_m1_api import login

pytestmark = pytest.mark.infra


@pytest.fixture
async def cp(db: Database) -> httpx.AsyncClient:
    app = create_app(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client


async def test_ops_summary(cp: httpx.AsyncClient, seeded: Seed) -> None:
    admin = await login(cp, seeded, "admin")
    r = await cp.get("/api/v1/console/ops-summary", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "active_drifts" in body
    assert "open_risk_events" in body
    assert isinstance(body["drift_items"], list)
    assert isinstance(body["recent_risk_events"], list)
