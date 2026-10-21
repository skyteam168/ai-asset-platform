"""审计锚点 WORM 归档（architecture.md 12.4：锚定后同步对象锁桶）。"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.libs.common.config import Settings, get_settings
from aip.libs.storage.s3 import S3Client


class WormStore(Protocol):
    async def put_if_absent(self, key: str, body: bytes, *, content_type: str = "application/json") -> str: ...

    async def get(self, key: str) -> bytes: ...


class MemoryWormStore:
    """进程内不可覆写存储（单测）。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_if_absent(self, key: str, body: bytes, *, content_type: str = "application/json") -> str:
        if key in self.objects and self.objects[key] != body:
            raise RuntimeError(f"WORM 冲突：{key}")
        self.objects[key] = body
        return f"memory://{key}"

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


class S3WormStore:
    def __init__(self, client: S3Client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket
        self._ready = False

    async def _ensure(self) -> None:
        if not self._ready:
            await self.client.ensure_bucket(self.bucket)
            self._ready = True

    async def put_if_absent(self, key: str, body: bytes, *, content_type: str = "application/json") -> str:
        await self._ensure()
        return await self.client.put_if_absent(self.bucket, key, body, content_type=content_type)

    async def get(self, key: str) -> bytes:
        await self._ensure()
        return await self.client.get(self.bucket, key)


def worm_object_key(shard_id: str, up_to_seq: int) -> str:
    return f"audit-anchors/{shard_id}/{up_to_seq}.json"


def anchor_payload(
    *,
    shard_id: str,
    up_to_seq: int,
    head_hash: str,
    signature: str,
    key_id: str,
    anchored_at: str | None = None,
) -> bytes:
    doc: dict[str, Any] = {
        "shard_id": shard_id,
        "up_to_seq": up_to_seq,
        "head_hash": head_hash,
        "signature": signature,
        "key_id": key_id,
    }
    if anchored_at:
        doc["anchored_at"] = anchored_at
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


async def sync_anchors(conn: AsyncConnection, store: WormStore) -> list[str]:
    """把尚未对应对象（或可幂等重放）的锚点写入 WORM；返回 object URI 列表。"""
    rows = (
        await conn.execute(
            text(
                "SELECT shard_id, up_to_seq, head_hash, signature, key_id, anchored_at "
                "FROM audit.audit_anchor ORDER BY shard_id, up_to_seq"
            )
        )
    ).mappings().all()
    uris: list[str] = []
    for r in rows:
        key = worm_object_key(str(r["shard_id"]), int(r["up_to_seq"]))
        body = anchor_payload(
            shard_id=str(r["shard_id"]),
            up_to_seq=int(r["up_to_seq"]),
            head_hash=str(r["head_hash"]),
            signature=str(r["signature"]),
            key_id=str(r["key_id"]),
            anchored_at=r["anchored_at"].isoformat().replace("+00:00", "Z") if r["anchored_at"] else None,
        )
        uris.append(await store.put_if_absent(key, body))
    return uris


async def verify_worm(conn: AsyncConnection, store: WormStore) -> list[str]:
    """核对库内每个锚点在 WORM 中存在且字段一致；返回问题描述。"""
    problems: list[str] = []
    rows = (
        await conn.execute(
            text("SELECT shard_id, up_to_seq, head_hash, signature, key_id FROM audit.audit_anchor")
        )
    ).mappings().all()
    for r in rows:
        key = worm_object_key(str(r["shard_id"]), int(r["up_to_seq"]))
        try:
            raw = await store.get(key)
        except FileNotFoundError:
            problems.append(f"{key}: 缺失")
            continue
        try:
            doc = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            problems.append(f"{key}: 非 JSON")
            continue
        for field in ("head_hash", "signature", "key_id"):
            if str(doc.get(field)) != str(r[field]):
                problems.append(f"{key}: {field} 不一致")
                break
        if int(doc.get("up_to_seq", -1)) != int(r["up_to_seq"]) or str(doc.get("shard_id")) != str(r["shard_id"]):
            problems.append(f"{key}: 主键字段不一致")
    return problems


def worm_store_from_settings(settings: Settings | None = None) -> WormStore | None:
    s = settings or get_settings()
    if not s.worm_enabled:
        return None
    client = S3Client(s.s3_endpoint, s.s3_access_key, s.s3_secret_key, region=s.s3_region)
    return S3WormStore(client, s.worm_bucket)
