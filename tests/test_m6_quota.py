"""M6 / P9：配额与计量。数据面计数在 PostgreSQL（多副本共享），条件自增：被拒的调用不消耗额度。"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aip.connectors.mock import MockConnector
from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import snapshot as iam_snapshot
from aip.libs.audit.spool import AuditSpool
from aip.libs.audit.store import ship
from iam_seed import Seed, app_url
from m2_fixtures import Published, publish_tool
from m3_world import CONSUMER, World, connector, tool, write_tool
from test_m1_api import login

pytestmark = pytest.mark.infra
READ, WRITE = "finance.erp.get_po", "finance.erp.approve_po"
OPEN = {"type": "object"}


@pytest.fixture
async def engine(db: Database) -> AsyncIterator[AsyncEngine]:
    e = create_async_engine(app_url())
    yield e
    await e.dispose()


async def world(
    tmp: pathlib.Path, engine: AsyncEngine, quota: dict[str, int], ceiling: dict[str, int] | None = None
) -> World:
    MockConnector.executed.clear()
    w = World(
        tmp,
        [
            tool(READ, "erp", "q", input_schema=OPEN, output_schema=OPEN, scope_binding={}),
            write_tool(WRITE, "erp", "w", input_schema=OPEN, scope_binding={}),
        ],
        [
            connector(
                "erp", "mock", {"operations": {"q": {"response": {"ok": 1}}, "w": {"response": {}, "write": True}}}
            )
        ],
        runtime_engine=engine,
    )
    await w.ready()
    data = w.app.state.iam.data
    data["consumers"][CONSUMER]["quota"] = quota
    for i, g in enumerate(data["grants"]):
        g["ceiling_id"] = "c-shared" if ceiling else f"c{i}"
        g["ceiling_quota"] = ceiling or {}
    return w


def calls(w: World, op: str) -> int:
    return int(w.connector("erp").calls.get(op, 0))


async def test_daily_calls_and_idempotent_retry(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await world(tmp_path, engine, {"daily_calls": 3})
    p = w.principal()
    results = [await w.runtime.invoke(p, READ, {}) for _ in range(3)]
    assert [r["status"] for r in results] == ["succeeded"] * 3
    over = await w.runtime.invoke(p, READ, {})
    assert over["status"] == "failed" and over["error"]["code"] == "RATE_LIMITED"
    assert "消费方每日调用" in over["error"]["message"] and over["next_action"]["type"] == "wait"
    assert calls(w, "q") == 3  # 超限的调用没有到达下游
    # 换一个 World 使用同一计数表（模拟另一副本）：同样已满
    other = await world(tmp_path / "b", engine, {"daily_calls": 3})
    assert (await other.runtime.invoke(other.principal(), READ, {}))["error"]["code"] == "RATE_LIMITED"


async def test_write_quota_and_retry_does_not_consume(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await world(tmp_path, engine, {"daily_writes": 1})
    p = w.principal()
    first = await w.runtime.invoke(p, WRITE, {}, idempotency_key="k1")
    retry = await w.runtime.invoke(p, WRITE, {}, idempotency_key="k1")
    assert first["status"] == retry["status"] == "succeeded" and first["execution_id"] == retry["execution_id"]
    second = await w.runtime.invoke(p, WRITE, {}, idempotency_key="k2")
    assert second["error"]["code"] == "RATE_LIMITED" and "写操作" in second["error"]["message"]
    assert (await w.runtime.invoke(p, READ, {}))["status"] == "succeeded"  # 读操作不受写配额限制
    assert calls(w, "w") == 1


async def test_ceiling_quota_shared_and_concurrent(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await world(tmp_path, engine, {}, ceiling={"daily_calls": 5})
    p = w.principal()
    results = await asyncio.gather(*(w.runtime.invoke(p, READ, {}) for _ in range(9)))
    ok = [r for r in results if r["status"] == "succeeded"]
    limited = [r for r in results if (r["error"] or {}).get("code") == "RATE_LIMITED"]
    assert (len(ok), len(limited)) == (5, 4)
    assert "授权上限每日调用" in limited[0]["error"]["message"]
    # 同一上限下的另一个能力共享额度
    assert (await w.runtime.invoke(p, WRITE, {}, idempotency_key="w1"))["error"]["code"] == "RATE_LIMITED"


async def test_quota_api_snapshot_and_usage(db: Database, seeded: Seed, tmp_path: pathlib.Path) -> None:
    published: Published = await publish_tool(db, seeded)
    app = create_cp(db=Database(app_url()))
    cp = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp")
    admin = await login(cp, seeded, "admin")
    vn = await login(cp, seeded, "vn_admin")
    kh = await login(cp, seeded, "kh_admin")
    consumer = seeded.consumers["vn-ai"]
    ok = await cp.put(
        f"/api/v1/consumers/{consumer}/quota", json={"daily_calls": 1000, "daily_writes": 50}, headers=admin
    )
    assert ok.status_code == 200 and ok.json()["quota"] == {"daily_calls": 1000, "daily_writes": 50}
    assert (await cp.put(f"/api/v1/consumers/{consumer}/quota", json={"daily_calls": 1}, headers=vn)).status_code == 403
    bad = await cp.put(f"/api/v1/consumers/{consumer}/quota", json={"monthly": 5}, headers=admin)
    assert bad.status_code == 400 and "未知的配额项" in bad.json()["detail"]
    neg = await cp.put(f"/api/v1/consumers/{consumer}/quota", json={"daily_calls": -1}, headers=admin)
    assert neg.status_code == 400
    r = await cp.put(f"/api/v1/grant-ceilings/{published.ceiling_vn}/quota", json={"daily_calls": 20000}, headers=admin)
    assert r.status_code == 200
    async with db.tx(DbScope.system()) as conn:
        snap = await iam_snapshot.build_payload(conn)
    assert snap["consumers"][str(consumer)]["quota"] == {"daily_calls": 1000, "daily_writes": 50}
    [g] = [g for g in snap["grants"] if g["id"] == str(published.grant_vn)]
    assert g["ceiling_quota"] == {"daily_calls": 20000} and g["ceiling_id"] == str(published.ceiling_vn)

    # 计量：审计里的执行记录按日、公司、消费方、能力汇总
    spool = AuditSpool(tmp_path / "s", f"dp-{uuid.uuid4().hex[:6]}")
    now = dt.datetime.now(dt.UTC)
    recs: list[tuple[Any, dict[str, Any]]] = [
        (seeded.vn, {"consumer_id": str(consumer), "capability_id": "x.y.z", "status": "succeeded", "write": True}),
        (seeded.vn, {"consumer_id": str(consumer), "capability_id": "x.y.z", "status": "failed", "write": False}),
        (seeded.kh, {"consumer_id": str(seeded.consumers["kh-ai"]), "capability_id": "x.y.z", "status": "succeeded"}),
    ]
    for i, (company, rec) in enumerate(recs):
        spool.append("tool.execution", company_id=company, record=rec, now=now - dt.timedelta(seconds=10 - i))
    await ship(spool, db.engine)
    rows = (await cp.get("/api/v1/usage", headers=admin)).json()["items"]
    vn_row = next(r for r in rows if r["consumer_id"] == str(consumer))
    assert (vn_row["calls"], vn_row["failed"], vn_row["writes"]) == (2, 1, 1)
    assert len(rows) == 2
    assert [r["company_id"] for r in (await cp.get("/api/v1/usage", headers=vn)).json()["items"]] == [str(seeded.vn)]
    assert [r["company_id"] for r in (await cp.get("/api/v1/usage", headers=kh)).json()["items"]] == [str(seeded.kh)]
