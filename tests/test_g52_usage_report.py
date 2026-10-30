"""G5.2：usage_counter 成本分摊报表。"""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import text

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.billing.usage_report import aggregate_usage_counter, parse_day
from aip.control_plane.db import Database, DbScope
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import PASSWORD, Seed


def test_parse_day_rejects_bad() -> None:
    with pytest.raises(PlatformError) as ei:
        parse_day("2026/01/01", field="from")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_aggregate_rules(db: Database, seeded: Seed) -> None:
    cid = seeded.hq
    consumer = uuid.uuid4()
    asset = uuid.uuid4()
    ver1, ver2 = uuid.uuid4(), uuid.uuid4()
    day = dt.date.today()
    async with db.tx(DbScope.system()) as conn:
        await conn.execute(text("DELETE FROM exec.usage_counter"))
        for key, n in (
            (f"consumer:{consumer}", 10),
            (f"consumer_write:{consumer}", 2),
            (f"asset:{asset}:version:{ver1}", 4),
            (f"asset:{asset}:version:{ver2}", 6),
            (f"ceiling:{uuid.uuid4()}", 99),
        ):
            await conn.execute(
                text(
                    "INSERT INTO exec.usage_counter (day, key, company_id, count) "
                    "VALUES (:d, :k, :c, :n)"
                ),
                {"d": day, "k": key, "c": cid, "n": n},
            )
        by_co = await aggregate_usage_counter(
            conn, day_from=day, day_to=day, group_by="company"
        )
        assert by_co[0]["dim"] == str(cid)
        assert by_co[0]["calls"] == 10 + 2 + 4 + 6 + 99

        by_cons = await aggregate_usage_counter(
            conn, day_from=day, day_to=day, group_by="consumer"
        )
        assert len(by_cons) == 1
        assert by_cons[0]["dim"] == str(consumer)
        assert by_cons[0]["calls"] == 10 and by_cons[0]["writes"] == 2

        by_asset = await aggregate_usage_counter(
            conn, day_from=day, day_to=day, group_by="asset"
        )
        assert len(by_asset) == 1
        assert by_asset[0]["dim"] == str(asset) and by_asset[0]["calls"] == 10


async def _login(cp: httpx.AsyncClient, seed: Seed) -> dict[str, str]:
    r = await cp.post(
        "/api/v1/auth/login",
        json={"login_name": "admin", "password": PASSWORD, "totp": seed.code("admin")},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


@pytest.mark.asyncio
async def test_console_usage_report_http(db: Database, seeded: Seed) -> None:
    day = dt.date.today()
    day_s = day.isoformat()
    async with db.tx(DbScope.system()) as conn:
        await conn.execute(
            text(
                "INSERT INTO exec.usage_counter (day, key, company_id, count) "
                "VALUES (:d, :k, :c, 5) ON CONFLICT (day, key) DO UPDATE SET count = 5"
            ),
            {"d": day, "k": f"consumer:{uuid.uuid4()}", "c": seeded.hq},
        )
    transport = httpx.ASGITransport(app=create_cp(db))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cp:
        h = await _login(cp, seeded)
        r = await cp.get(
            "/api/v1/console/usage-report",
            headers=h,
            params={"group_by": "company", "from": day_s, "to": day_s},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["group_by"] == "company"
        assert any(it["company_id"] == str(seeded.hq) and it["calls"] >= 5 for it in body["items"])
        bad = await cp.get(
            "/api/v1/console/usage-report",
            headers=h,
            params={"group_by": "team"},
        )
        assert bad.status_code == 400
