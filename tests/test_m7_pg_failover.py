"""M7k：PostgreSQL 切换窗口审计零丢失（architecture.md 12.2 / M7）。

CI 不启真实主从：用死连接模拟切换窗口；人工主从见 docs/ops/pg-failover.md。
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aip.control_plane.db import Database, DbScope
from aip.libs.audit.spool import AuditSpool
from aip.libs.audit.store import ship, verify
from aip.libs.tokens.keys import SigningKey
from m3_world import World, connector, tool

pytestmark = pytest.mark.infra

KEY = SigningKey.generate()
DAY = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)
CAP = "production.mes.query_work_orders"
ARGS = {"factory_id": "F001", "status": "open"}
STATIC = "aipk_m7k_pg"


def fill(spool: AuditSpool, n: int, *, start: int = 0, now: dt.datetime = DAY) -> None:
    for i in range(n):
        spool.append(
            "pdp.decision",
            company_id=None,
            now=now + dt.timedelta(seconds=start + i),
            record={"i": start + i, "decision": "allow", "reasons": [], "failover": True},
        )


async def problems(db: Database) -> list[str]:
    async with db.tx(DbScope()) as conn:
        return [str(p) for p in await verify(conn, [KEY])]


async def count_audit(db: Database) -> int:
    async with db.tx(DbScope()) as conn:
        return int((await conn.execute(text("SELECT count(*) FROM audit.audit_log"))).scalar_one())


async def test_ship_resumes_after_failover_window(db: Database, tmp_path: pathlib.Path) -> None:
    """切换窗口：ship 失败不丢 spool；窗口内继续 append；恢复后一次 ship 全部入库且链完整。"""
    spool = AuditSpool(tmp_path, "dp-m7k-a")
    fill(spool, 4, start=0)
    assert await ship(spool, db.engine) == 4
    baseline = await count_audit(db)

    fill(spool, 3, start=10)  # 待入库
    dead = create_async_engine("postgresql+asyncpg://aip_app:x@127.0.0.1:9/aip")
    with pytest.raises(OSError):
        await ship(spool, dead)
    await dead.dispose()

    fill(spool, 2, start=20)  # 主库不可用期间继续写 spool
    assert spool.unshipped_bytes() > 0

    assert await ship(spool, db.engine) == 5
    assert await ship(spool, db.engine) == 0
    assert spool.unshipped_bytes() == 0
    assert await count_audit(db) == baseline + 5
    assert await problems(db) == []


async def test_ship_transaction_rollback_keeps_offset(db: Database, tmp_path: pathlib.Path) -> None:
    """半批失败等价：事务回滚则 offset 不前进，恢复后重投不丢不乱序。"""
    spool = AuditSpool(tmp_path, "dp-m7k-b")
    fill(spool, 6, start=0)
    dead = create_async_engine("postgresql+asyncpg://aip_app:x@127.0.0.1:9/aip")
    with pytest.raises(OSError):
        await ship(spool, dead, batch=2)
    await dead.dispose()

    off = list(tmp_path.glob("*.offset"))
    assert not off or int(off[0].read_text()) == 0

    assert await ship(spool, db.engine, batch=2) == 6
    assert await problems(db) == []
    assert await count_audit(db) == 6


async def test_invoke_buffers_audit_during_db_outage(db: Database, tmp_path: pathlib.Path) -> None:
    """调用路径：审计库断连时 invoke 仍成功并累积 spool；恢复后 ship 零丢失。"""
    w = World(
        tmp_path,
        [tool(CAP, "mes", "q")],
        [connector("mes", "mock", {"operations": {"q": {"response": [{"wo_no": "W1"}]}}})],
        static_token=STATIC,
        exposure=["rest"],
    )
    await w.ready()
    before = w.app.state.audit.unshipped_bytes()

    r = await w.runtime.invoke(w.principal(), CAP, ARGS)
    assert r["status"] == "succeeded", r
    assert w.app.state.audit.unshipped_bytes() > before

    dead = create_async_engine("postgresql+asyncpg://aip_app:x@127.0.0.1:9/aip")
    with pytest.raises(OSError):
        await ship(w.app.state.audit, dead)
    await dead.dispose()

    r2 = await w.runtime.invoke(w.principal(), CAP, ARGS)
    assert r2["status"] == "succeeded", r2
    buffered = w.app.state.audit.unshipped_bytes()
    assert buffered > 0

    n = await ship(w.app.state.audit, db.engine)
    assert n > 0
    assert w.app.state.audit.unshipped_bytes() == 0
    assert await problems(db) == []
