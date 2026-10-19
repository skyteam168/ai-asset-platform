"""IAM 快照（architecture.md 4.4、5.1）：控制面打包身份与授权事实，签名后由数据面拉取。

全量快照；授权事实（grants）由 grant 模块经 snapshot_part() 提供，iam 不读其表。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.grant import service as grant_service
from aip.control_plane.iam import tables as t
from aip.libs.common.ids import utcnow
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.signed import sign_payload

FORMAT = 1


def _s(v: object) -> str | None:
    return None if v is None else str(v)


async def build_payload(conn: AsyncConnection) -> dict[str, Any]:
    """需在系统上下文（bypass RLS）中调用。只导出启用的组织与未过期的授权事实。"""
    now = utcnow()
    orgs = {r.id: r for r in (await conn.execute(select(t.org_unit))).all()}
    active = {oid for oid, r in orgs.items() if r.status == "active"}
    closure: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for a, d in (await conn.execute(select(t.org_closure.c.ancestor_id, t.org_closure.c.descendant_id))).all():
        if a in active and d in active:
            closure[a].add(d)
    # 停用节点的后代即使自身 active 也不再可用
    for oid, r in orgs.items():
        if r.status != "active":
            for d in closure.pop(oid, set()):
                active.discard(d)
    closure = {a: {d for d in ds if d in active} for a, ds in closure.items() if a in active}

    def default_clearance(org_id: uuid.UUID) -> int:
        node = orgs.get(org_id)
        while node is not None:
            if node.default_clearance:
                return int(node.default_clearance)
            node = orgs.get(node.parent_id) if node.parent_id else None
        return 1

    memberships: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for uid, oid in (await conn.execute(select(t.user_org_membership.c.user_id, t.user_org_membership.c.org_id))).all():
        memberships[uid].add(oid)
    extra: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for uid, oid in (
        await conn.execute(
            select(t.user_data_scope.c.user_id, t.user_data_scope.c.org_id).where(
                or_(t.user_data_scope.c.expires_at.is_(None), t.user_data_scope.c.expires_at > now)
            )
        )
    ).all():
        extra[uid].add(oid)
    roles: dict[uuid.UUID, list[dict[str, str]]] = defaultdict(list)
    for rr in (
        await conn.execute(
            select(t.user_role).where(or_(t.user_role.c.expires_at.is_(None), t.user_role.c.expires_at > now))
        )
    ).mappings():
        roles[rr["user_id"]].append({"role": rr["role"], "scope_org_id": str(rr["scope_org_id"])})

    users: dict[str, Any] = {}
    employee_index: dict[str, dict[str, str]] = defaultdict(dict)
    for u in (await conn.execute(select(t.iam_user))).mappings():
        status = u["status"] if u["home_org_id"] in active else "disabled"
        override = (
            u["clearance_override"]
            if u["clearance_override"] and (u["clearance_expires_at"] is None or u["clearance_expires_at"] > now)
            else None
        )
        scope: set[uuid.UUID] = set()
        for oid in memberships[u["id"]] | extra[u["id"]]:
            scope |= closure.get(oid, set())
        users[str(u["id"])] = {
            "company_id": str(u["company_id"]),
            "home_org_id": str(u["home_org_id"]),
            "status": status,
            "clearance": int(override) if override else default_clearance(u["home_org_id"]),
            "org_ids": sorted(str(a) for a, ds in closure.items() if u["home_org_id"] in ds),
            "data_scope_org_ids": sorted(str(x) for x in scope),
            "roles": sorted(roles[u["id"]], key=lambda x: (x["role"], x["scope_org_id"])),
        }
        if u["employee_no"] and status == "active":
            employee_index[str(u["company_id"])][u["employee_no"]] = str(u["id"])

    consumers: dict[str, Any] = {}
    for c in (await conn.execute(select(t.consumer))).mappings():
        consumers[str(c["id"])] = {
            "client_id": c["client_id"],
            "company_id": str(c["company_id"]),
            "clearance_cap": int(c["clearance_cap"]),
            "bind_policy": c["bind_policy"],
            "status": c["status"] if c["company_id"] in active else "disabled",
            "jwks": {},
            "static_tokens": {},
            "bindings": {},
            "agent_profiles": {},
        }
    for k in (
        await conn.execute(
            select(t.consumer_jwk).where(
                and_(
                    t.consumer_jwk.c.status == "active",
                    or_(t.consumer_jwk.c.expires_at.is_(None), t.consumer_jwk.c.expires_at > now),
                )
            )
        )
    ).mappings():
        consumers[str(k["consumer_id"])]["jwks"][k["kid"]] = k["jwk"]
    for k in (
        await conn.execute(
            select(t.consumer_credential).where(
                and_(
                    t.consumer_credential.c.status == "active",
                    t.consumer_credential.c.kind == "static_token",
                    t.consumer_credential.c.expires_at > now,
                )
            )
        )
    ).mappings():
        consumers[str(k["consumer_id"])]["static_tokens"][k["token_hash"]] = {
            "expires_at": k["expires_at"].isoformat(),
            "ip_binding": k["ip_binding"] or [],
        }
    for b in (await conn.execute(select(t.user_binding).where(t.user_binding.c.expires_at > now))).mappings():
        consumers[str(b["consumer_id"])]["bindings"][b["external_subject"]] = str(b["user_id"])
    for p in (await conn.execute(select(t.agent_profile))).mappings():
        consumers[str(p["consumer_id"])]["agent_profiles"][p["name"]] = str(p["id"])

    external_codes: dict[str, dict[str, str]] = defaultdict(dict)
    for ec in (await conn.execute(select(t.org_external_code))).mappings():
        if ec["org_id"] in active:
            external_codes[ec["system_key"]][ec["external_code"]] = str(ec["org_id"])

    return {
        "format": FORMAT,
        "generated_at": now.isoformat(),
        "orgs": {
            str(oid): {"code": r.code, "type": r.type, "parent_id": _s(r.parent_id), "company_id": str(r.company_id)}
            for oid, r in orgs.items()
            if oid in active
        },
        "closure": {str(a): sorted(str(d) for d in ds) for a, ds in closure.items()},
        "users": users,
        "employee_index": employee_index,
        "consumers": consumers,
        # scope_binding by_code：旧系统编码 → 平台节点（architecture.md 2.3）
        "external_codes": external_codes,
        **await grant_service.snapshot_part(conn),
    }


def sign(payload: dict[str, Any], key: SigningKey) -> tuple[str, str]:
    return sign_payload(payload, key)


async def publish(conn: AsyncConnection, key: SigningKey) -> dict[str, Any] | None:
    """有新领域事件时生成下一份快照；返回快照记录（含 seq），无变化返回 None。"""
    event_seq = (await conn.execute(select(func.coalesce(func.max(t.domain_event.c.seq), 0)))).scalar_one()
    last = (
        await conn.execute(
            select(t.iam_snapshot.c.seq, t.iam_snapshot.c.event_seq).order_by(t.iam_snapshot.c.seq.desc()).limit(1)
        )
    ).first()
    if last is not None and last.event_seq >= event_seq:
        return None
    seq = (last.seq if last else 0) + 1
    payload = await build_payload(conn)
    payload["seq"] = seq
    digest, signature = sign(payload, key)
    await conn.execute(
        t.iam_snapshot.insert().values(
            seq=seq, event_seq=event_seq, sha256=digest, signature=signature, payload=payload
        )
    )
    return {"kind": "iam", "seq": seq, "full": True, "sha256": digest, "signature": signature, "payload": payload}


async def latest(conn: AsyncConnection, after_seq: int = 0) -> dict[str, Any] | None:
    row = (
        (
            await conn.execute(
                select(t.iam_snapshot)
                .where(t.iam_snapshot.c.seq > after_seq)
                .order_by(t.iam_snapshot.c.seq.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        "kind": "iam",
        "seq": row["seq"],
        "full": True,
        "sha256": row["sha256"],
        "signature": row["signature"],
        "payload": row["payload"],
    }
