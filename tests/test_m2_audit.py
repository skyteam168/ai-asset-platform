"""M2 / P4：审计 spool、分片哈希链、入库、锚定、篡改检测（architecture.md 12.4）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import pathlib
import subprocess
import sys

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aip.control_plane.db import Database, DbScope
from aip.libs.audit.spool import AuditSpool
from aip.libs.audit.store import anchor, ship, verify, verify_spool_file
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import SigningKey
from iam_seed import ROOT, app_url, owner_dsn

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()
DAY1 = dt.datetime(2026, 10, 18, 23, 59, tzinfo=dt.UTC)
DAY2 = dt.datetime(2026, 10, 19, 0, 1, tzinfo=dt.UTC)


def fill(spool: AuditSpool, n: int, now: dt.datetime = DAY2) -> None:
    for i in range(n):
        spool.append(
            "pdp.decision",
            company_id=None,
            now=now + dt.timedelta(seconds=i),
            record={"i": i, "decision": "allow", "reasons": [], "score": 1.5},
        )


async def shipped(db: Database, spool: AuditSpool) -> None:
    assert await ship(spool, db.engine) > 0


async def problems(db: Database) -> list[str]:
    async with db.tx(DbScope()) as conn:
        return [str(p) for p in await verify(conn, [KEY])]


async def tamper(sql: str) -> None:
    """模拟拥有超级用户权限的攻击者：关闭追加保护触发器后改写数据。"""
    conn = await asyncpg.connect(owner_dsn())
    try:
        await conn.execute("SET session_replication_role = replica")  # 跳过触发器
        await conn.execute(sql)
    finally:
        await conn.close()


async def test_spool_chain_ship_anchor_and_verify(db: Database, tmp_path: pathlib.Path) -> None:
    spool = AuditSpool(tmp_path, "dp-a")
    fill(spool, 3, DAY1)
    fill(spool, 5, DAY2)  # 跨日 → 新 shard，新链
    files = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert files == ["dp-a-20261018.jsonl", "dp-a-20261019.jsonl"]
    assert all(not verify_spool_file(p) for p in tmp_path.glob("*.jsonl"))
    await shipped(db, spool)
    async with db.tx(DbScope()) as conn:
        assert await anchor(conn, KEY) == 2
        assert await anchor(conn, KEY) == 0  # 幂等
        count = (await conn.execute(text("SELECT count(*) FROM audit.audit_log"))).scalar_one()
    assert count == 8
    assert await problems(db) == []


async def test_tampering_one_row_is_detected_with_shard_and_seq(db: Database, tmp_path: pathlib.Path) -> None:
    """验收：篡改一行审计后校验脚本报错并定位到 shard 与 seq。"""
    spool = AuditSpool(tmp_path, "dp-b")
    fill(spool, 6)
    await shipped(db, spool)
    async with db.tx(DbScope()) as conn:
        await anchor(conn, KEY)
    await tamper(
        "UPDATE audit.audit_log SET record = jsonb_set(record, '{decision}', '\"deny\"') "
        "WHERE shard_id = 'dp-b-20261019' AND seq = 3"
    )
    found = await problems(db)
    assert found == ["shard=dp-b-20261019 seq=3: 内容哈希不匹配（记录被篡改）"]
    env = {**os.environ, "AIP_APP_DATABASE_URL": app_url(), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, "tools/audit_verify.py", "--dsn", app_url()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert r.returncode == 1 and "seq=3" in r.stdout


async def test_deleting_or_rewriting_tail_is_detected_by_anchor(db: Database, tmp_path: pathlib.Path) -> None:
    spool = AuditSpool(tmp_path, "dp-c")
    fill(spool, 4)
    await shipped(db, spool)
    async with db.tx(DbScope()) as conn:
        await anchor(conn, KEY)
    await tamper("DELETE FROM audit.audit_log WHERE shard_id = 'dp-c-20261019' AND seq = 4")
    assert await problems(db) == ["shard=dp-c-20261019 seq=4: 锚定的记录已不存在（被删除）"]
    await tamper("DELETE FROM audit.audit_log WHERE shard_id = 'dp-c-20261019' AND seq = 2")
    assert "shard=dp-c-20261019 seq=2: 缺失记录（下一条是 seq=3）" in await problems(db)


async def test_forged_anchor_signature_detected(db: Database, tmp_path: pathlib.Path) -> None:
    spool = AuditSpool(tmp_path, "dp-d")
    fill(spool, 2)
    await shipped(db, spool)
    async with db.tx(DbScope()) as conn:
        await anchor(conn, SigningKey.generate())  # 攻击者自己的密钥
    assert await problems(db) == ["shard=dp-d-20261019 seq=2: 锚点签名无效"]


async def test_app_role_cannot_update_or_delete_audit(db: Database, tmp_path: pathlib.Path) -> None:
    spool = AuditSpool(tmp_path, "dp-e")
    fill(spool, 1)
    await shipped(db, spool)
    for sql in ("UPDATE audit.audit_log SET kind = 'x'", "DELETE FROM audit.audit_log", "TRUNCATE audit.audit_log"):
        with pytest.raises(Exception, match=r"permission denied|append-only"):
            async with db.tx(DbScope()) as conn:
                await conn.execute(text(sql))
    owner = await asyncpg.connect(owner_dsn())
    try:
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await owner.execute("UPDATE audit.audit_log SET kind = 'x'")
    finally:
        await owner.close()


async def test_ship_is_idempotent_and_resumes_after_db_outage(db: Database, tmp_path: pathlib.Path) -> None:
    spool = AuditSpool(tmp_path, "dp-f")
    fill(spool, 3)
    dead = create_async_engine("postgresql+asyncpg://aip_app:x@127.0.0.1:9/aip")
    with pytest.raises(OSError):
        await ship(spool, dead)
    await dead.dispose()
    fill(spool, 2, DAY2 + dt.timedelta(minutes=5))  # 审计库不可用期间继续写 spool
    assert spool.unshipped_bytes() > 0
    assert await ship(spool, db.engine) == 5
    assert await ship(spool, db.engine) == 0
    (tmp_path / "dp-f-20261019.offset").write_text("0")  # 模拟 offset 丢失：重复投递被主键去重
    await ship(spool, db.engine)
    async with db.tx(DbScope()) as conn:
        assert (await conn.execute(text("SELECT count(*) FROM audit.audit_log"))).scalar_one() == 5
    assert spool.unshipped_bytes() == 0
    assert await problems(db) == []


async def test_spool_backpressure(tmp_path: pathlib.Path) -> None:
    spool = AuditSpool(tmp_path, "dp-g", max_bytes=2000)
    while not spool.write_blocked:
        fill(spool, 1)
    assert spool.unshipped_bytes() >= 1600
    with pytest.raises(PlatformError) as e:
        for _ in range(50):
            fill(spool, 1)
    assert e.value.code == ErrorCode.POLICY_UNAVAILABLE


async def test_restart_continues_chain(db: Database, tmp_path: pathlib.Path) -> None:
    fill(AuditSpool(tmp_path, "dp-h"), 2)
    fill(AuditSpool(tmp_path, "dp-h"), 2, DAY2 + dt.timedelta(minutes=1))  # 新进程实例
    assert not verify_spool_file(tmp_path / "dp-h-20261019.jsonl")
    await shipped(db, AuditSpool(tmp_path, "dp-h"))
    assert await problems(db) == []


async def test_partition_created_on_demand(db: Database, tmp_path: pathlib.Path) -> None:
    spool = AuditSpool(tmp_path, "dp-i")
    fill(spool, 1, dt.datetime(2027, 3, 2, tzinfo=dt.UTC))
    await shipped(db, spool)
    async with db.tx(DbScope()) as conn:
        part = (
            await conn.execute(
                text("SELECT tableoid::regclass::text FROM audit.audit_log WHERE shard_id = 'dp-i-20270302'")
            )
        ).scalar_one()
    assert part == "audit.audit_log_202703"


async def test_data_plane_background_shipper(
    db: Database, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据面后台任务：决策审计自动入库并锚定。"""
    from aip.data_plane.app import create_app
    from aip.libs.common import config
    from aip.libs.tokens.replay import MemoryReplayStore

    monkeypatch.setenv("AIP_AUDIT_DATABASE_URL", app_url())
    monkeypatch.setenv("AIP_AUDIT_SHIP_INTERVAL_S", "0.1")
    monkeypatch.setenv("AIP_AUDIT_ANCHOR_INTERVAL_S", "0")
    config.get_settings.cache_clear()
    try:
        app = create_app(key=KEY, replay=MemoryReplayStore(), data_dir=tmp_path)
        async with app.router.lifespan_context(app):
            await app.state.policy.decide(None, "x.y.z", {})
            for _ in range(50):
                async with db.tx(DbScope()) as conn:
                    n = (await conn.execute(text("SELECT count(*) FROM audit.audit_anchor"))).scalar_one()
                if n:
                    break
                await asyncio.sleep(0.1)
    finally:
        config.get_settings.cache_clear()
    assert n >= 1
    assert await problems(db) == []


async def test_group_commit_keeps_chain_intact_under_concurrency(tmp_path: pathlib.Path) -> None:
    """并发 append_async：一次 fsync 覆盖多条记录，链仍连续可校验，且每条返回时都已落盘。"""
    spool = AuditSpool(tmp_path, "dp-gc")
    drains = 0
    original = spool._drain

    def counting_drain() -> dict[str, int]:
        nonlocal drains
        drains += 1
        return original()

    spool._drain = counting_drain  # type: ignore[method-assign]
    lines = await asyncio.gather(*[spool.append_async("pdp.decision", record={"i": i}, now=DAY2) for i in range(200)])
    assert sorted(x["seq"] for x in lines) == list(range(1, 201))
    path = tmp_path / "dp-gc-20261019.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 200
    assert verify_spool_file(path) == []
    assert drains < 200, f"组提交应合并 fsync，实际 {drains} 次"
