"""H14：审计锚点 WORM 归档。"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from aip.control_plane.db import Database, DbScope
from aip.libs.audit.spool import AuditSpool
from aip.libs.audit.store import anchor, ship
from aip.libs.storage.worm import MemoryWormStore, sync_anchors, verify_worm, worm_object_key
from aip.libs.tokens.keys import SigningKey

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()
NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_memory_worm_put_if_absent_conflict() -> None:
    store = MemoryWormStore()
    await store.put_if_absent("k", b"a")
    await store.put_if_absent("k", b"a")
    with pytest.raises(RuntimeError, match="WORM"):
        await store.put_if_absent("k", b"b")


@pytest.mark.asyncio
async def test_anchor_sync_and_verify(db: Database, tmp_path: pathlib.Path) -> None:
    spool = AuditSpool(tmp_path, "dp-h14")
    spool.append("test.worm", company_id=None, now=NOW, record={"ok": True})
    assert await ship(spool, db.engine) == 1
    store = MemoryWormStore()
    async with db.tx(DbScope()) as conn:
        assert await anchor(conn, KEY, worm=store) == 1
        assert await verify_worm(conn, store) == []
        keys = list(store.objects)
        assert keys == [worm_object_key("dp-h14-20261021", 1)]
        doc = json.loads(store.objects[keys[0]].decode())
        assert doc["up_to_seq"] == 1 and "signature" in doc
        assert await sync_anchors(conn, store) == ["memory://audit-anchors/dp-h14-20261021/1.json"]


@pytest.mark.asyncio
async def test_seaweedfs_worm_roundtrip() -> None:
    """对本地 SeaweedFS（dev.py up）做 put_if_absent；无服务则跳过。"""
    import httpx

    from aip.libs.common.config import get_settings
    from aip.libs.storage.s3 import S3Client
    from aip.libs.storage.worm import S3WormStore

    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            await c.get(s.s3_endpoint)
    except httpx.HTTPError:
        pytest.skip("SeaweedFS 不可用")

    client = S3Client(s.s3_endpoint, s.s3_access_key, s.s3_secret_key, region=s.s3_region)
    store = S3WormStore(client, "aip-worm-test-h14")
    body = b'{"v":1,"n":"h14"}'
    try:
        uri = await store.put_if_absent("test/h14.json", body)
    except RuntimeError as e:
        pytest.skip(f"SeaweedFS S3 不可用: {e}")
    assert uri.startswith("s3://")
    assert await store.get("test/h14.json") == body
    await store.put_if_absent("test/h14.json", body)
    with pytest.raises(RuntimeError, match="WORM"):
        await store.put_if_absent("test/h14.json", b'{"v":2}')
    await client.aclose()
