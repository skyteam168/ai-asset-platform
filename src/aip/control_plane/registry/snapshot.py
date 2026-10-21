"""发布快照（architecture.md 5.1）：当前有效的 Release、版本 Spec 与策略 bundle，一起签名下发。"""

from __future__ import annotations

import hashlib
import pathlib
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.registry import tables as t
from aip.libs.common.ids import utcnow
from aip.libs.searchtext import build_search_text
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.signed import sign_payload

POLICY_DIR = pathlib.Path(__file__).resolve().parents[4] / "policies"
STATUS_FROM_STATE = {
    "approved": "canary",
    "canary": "canary",
    "active": "active",
    "deprecated": "deprecated",
    "suspended": "suspended",
    "retired": "retired",
}


def policy_bundle() -> dict[str, Any]:
    """策略 bundle：policies/aip 下的非测试 rego。revision = 内容摘要。"""
    modules = {
        p.relative_to(POLICY_DIR).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(POLICY_DIR.rglob("*.rego"))
        if not p.name.endswith("_test.rego")
    }
    digest = hashlib.sha256("".join(f"{k}\n{v}" for k, v in modules.items()).encode()).hexdigest()
    return {"revision": f"sha256:{digest}", "modules": modules}


async def build_payload(conn: AsyncConnection) -> dict[str, Any]:
    rows = await conn.execute(
        select(
            t.release,
            t.asset_version.c.semver,
            t.asset_version.c.spec,
            t.asset_version.c.content_hash,
            t.asset_version.c.state,
            t.asset.c.name,
            t.asset.c.type,
            t.asset.c.classification,
        )
        .select_from(
            t.release.join(t.asset_version, t.asset_version.c.id == t.release.c.version_id).join(
                t.asset, t.asset.c.id == t.release.c.asset_id
            )
        )
        .where(t.release.c.status == "active")
    )
    releases = []
    for r in rows.mappings():
        spec = r["spec"]
        entry = {
            "release_id": str(r["id"]),
            "asset_id": str(r["asset_id"]),
            "version_id": str(r["version_id"]),
            "asset_type": r["type"],
            "name": r["name"],
            "capability_id": spec.get("capability_id", r["name"]),
            "version": r["semver"],
            "content_hash": r["content_hash"],
            "env": r["env"],
            "channel": r["channel"],
            "scope": r["scope"],
            "exposure": list(r["exposure"]),
            "status": STATUS_FROM_STATE.get(r["state"], "suspended"),
            "classification": int(r["classification"]),
            "spec": spec,
        }
        entry["search_text"] = build_search_text(entry)
        releases.append(entry)
    releases.sort(key=lambda x: (x["capability_id"], x["env"], x["channel"]))
    from aip.control_plane.conn.service import snapshot_part as connections

    return {
        "format": 1,
        "generated_at": utcnow().isoformat(),
        "releases": releases,
        "policy": policy_bundle(),
        **await connections(conn),
    }


async def publish(conn: AsyncConnection, key: SigningKey) -> dict[str, Any] | None:
    event_seq = (await conn.execute(select(func.coalesce(func.max(t.domain_event.c.seq), 0)))).scalar_one()
    last = (
        await conn.execute(
            select(t.release_snapshot.c.seq, t.release_snapshot.c.event_seq, t.release_snapshot.c.policy_version)
            .order_by(t.release_snapshot.c.seq.desc())
            .limit(1)
        )
    ).first()
    bundle_rev = policy_bundle()["revision"]
    if last is not None and last.event_seq >= event_seq and last.policy_version == bundle_rev:
        return None
    seq = (last.seq if last else 0) + 1
    payload = await build_payload(conn)
    payload["seq"] = seq
    digest, signature = sign_payload(payload, key)
    await conn.execute(
        t.release_snapshot.insert().values(
            seq=seq, event_seq=event_seq, sha256=digest, signature=signature, policy_version=bundle_rev, payload=payload
        )
    )
    # 目录变更通知：订阅了 catalog.changed 的 webhook 入队（architecture.md 10.5）
    from aip.control_plane.webhook import service as wh

    await wh.enqueue(
        conn,
        event_kind="catalog.changed",
        body={"event": "catalog.changed", "payload": {"seq": seq, "sha256": digest}},
        event_id=f"release:{seq}",
    )
    return {"kind": "release", "seq": seq, "full": True, "sha256": digest, "signature": signature, "payload": payload}


async def latest(conn: AsyncConnection, after_seq: int = 0) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                select(t.release_snapshot)
                .where(t.release_snapshot.c.seq > after_seq)
                .order_by(t.release_snapshot.c.seq.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        "kind": "release",
        "seq": row["seq"],
        "full": True,
        "sha256": row["sha256"],
        "signature": row["signature"],
        "payload": row["payload"],
    }
