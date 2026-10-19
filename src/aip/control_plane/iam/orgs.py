"""组织树（architecture.md 2.1）：org_unit + org_closure + ltree 物化路径，平台是唯一权威。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.events import emit
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow

MAX_DEPTH = 6
CHILD_TYPES = {
    "group": {"company"},
    "company": {"department", "team"},
    "department": {"department", "team"},
    "team": {"team"},
}


@dataclass(frozen=True)
class Org:
    id: uuid.UUID
    type: str
    code: str
    name: str
    parent_id: uuid.UUID | None
    company_id: uuid.UUID
    default_clearance: int | None
    status: str
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in self.__dict__.items() if k != "path"}


def _label(org_id: uuid.UUID) -> str:
    return "n" + org_id.hex


async def get_org(conn: AsyncConnection, org_id: uuid.UUID) -> Org:
    row = (await conn.execute(select(t.org_unit).where(t.org_unit.c.id == org_id))).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "组织不存在或不可见")
    return Org(
        row["id"],
        row["type"],
        row["code"],
        row["name"],
        row["parent_id"],
        row["company_id"],
        row["default_clearance"],
        row["status"],
        str(row["path"]),
    )


async def list_orgs(conn: AsyncConnection, limit: int = 200) -> list[Org]:
    rows = (await conn.execute(select(t.org_unit).order_by(t.org_unit.c.path).limit(limit))).mappings().all()
    return [
        Org(
            r["id"],
            r["type"],
            r["code"],
            r["name"],
            r["parent_id"],
            r["company_id"],
            r["default_clearance"],
            r["status"],
            str(r["path"]),
        )
        for r in rows
    ]


async def descendants(conn: AsyncConnection, org_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await conn.execute(select(t.org_closure.c.descendant_id).where(t.org_closure.c.ancestor_id == org_id))
    return {r[0] for r in rows}


async def bootstrap_group(
    conn: AsyncConnection, *, group_code: str, group_name: str, hq_code: str, hq_name: str
) -> tuple[Org, Org]:
    """创建集团根与总部公司（只在初始化时调用，需系统上下文）。"""
    gid = new_id()
    await conn.execute(
        t.org_unit.insert().values(
            id=gid,
            type="group",
            code=group_code,
            name=group_name,
            parent_id=None,
            path=_label(gid),
            company_id=gid,
            status="active",
        )
    )
    await conn.execute(t.org_closure.insert().values(ancestor_id=gid, descendant_id=gid, depth=0, company_id=gid))
    await emit(conn, "org.created", gid, gid, None)
    hq = await create_org(
        conn, Actor.system_actor(), type_="company", code=hq_code, name=hq_name, parent_id=gid, default_clearance=2
    )
    return await get_org(conn, gid), hq


async def create_org(
    conn: AsyncConnection,
    actor: Actor,
    *,
    type_: str,
    code: str,
    name: str,
    parent_id: uuid.UUID,
    default_clearance: int | None = None,
    timezone: str | None = None,
    locale: str | None = None,
) -> Org:
    parent = await get_org(conn, parent_id)
    if parent.status != "active":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "父节点已停用")
    if type_ not in CHILD_TYPES[parent.type]:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"{parent.type} 下不能创建 {type_}")
    if parent.path.count(".") + 2 > MAX_DEPTH:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"组织深度不能超过 {MAX_DEPTH} 层")
    # 公司节点只能由平台管理员创建；其余节点需要管理父节点
    actor.require(actor.has("platform_admin") if type_ == "company" else actor.manages_org(parent_id))
    oid = new_id()
    company_id = oid if type_ == "company" else parent.company_id
    exists = await conn.execute(select(t.org_unit.c.id).where(t.org_unit.c.code == code))
    if exists.first() is not None:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"组织编码 {code} 已存在")
    await conn.execute(
        t.org_unit.insert().values(
            id=oid,
            type=type_,
            code=code,
            name=name,
            parent_id=parent_id,
            path=f"{parent.path}.{_label(oid)}",
            company_id=company_id,
            default_clearance=default_clearance,
            timezone=timezone,
            locale=locale,
            status="active",
        )
    )
    await conn.execute(
        text(
            "INSERT INTO iam.org_closure (ancestor_id, descendant_id, depth, company_id) "
            "SELECT ancestor_id, CAST(:oid AS uuid), depth + 1, CAST(:cid AS uuid) FROM iam.org_closure "
            "WHERE descendant_id = CAST(:pid AS uuid) "
            "UNION ALL SELECT CAST(:oid AS uuid), CAST(:oid AS uuid), 0, CAST(:cid AS uuid)"
        ),
        {"oid": oid, "pid": parent_id, "cid": company_id},
    )
    await emit(conn, "org.created", company_id, oid, actor.user_id, {"code": code})
    return await get_org(conn, oid)


async def move_org(conn: AsyncConnection, actor: Actor, org_id: uuid.UUID, new_parent_id: uuid.UUID) -> Org:
    node = await get_org(conn, org_id)
    parent = await get_org(conn, new_parent_id)
    if node.type in ("group", "company"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "集团与公司节点不能迁移")
    subtree = await descendants(conn, org_id)
    if new_parent_id in subtree:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "不能迁移到自己的子树下")
    if node.type not in CHILD_TYPES[parent.type] or parent.status != "active":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"不能迁移到 {parent.type} 下")
    cross_company = parent.company_id != node.company_id
    actor.require(
        actor.has("platform_admin")
        if cross_company
        else (actor.manages_org(org_id) and actor.manages_org(new_parent_id))
    )
    sub_depth = max(
        (
            await conn.execute(
                text("SELECT max(nlevel(path)) FROM iam.org_unit WHERE path <@ CAST(:p AS ltree)"), {"p": node.path}
            )
        ).scalar_one()
        - node.path.count(".")
        - 1,
        0,
    )
    if parent.path.count(".") + 2 + sub_depth > MAX_DEPTH:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"迁移后深度超过 {MAX_DEPTH} 层")

    ids = list(subtree)
    await conn.execute(
        text(
            "DELETE FROM iam.org_closure WHERE descendant_id = ANY(CAST(:ids AS uuid[])) "
            "AND NOT (ancestor_id = ANY(CAST(:ids AS uuid[])))"
        ),
        {"ids": ids},
    )
    await conn.execute(
        text(
            "INSERT INTO iam.org_closure (ancestor_id, descendant_id, depth, company_id) "
            "SELECT a.ancestor_id, s.descendant_id, a.depth + s.depth + 1, CAST(:cid AS uuid) "
            "FROM iam.org_closure a CROSS JOIN iam.org_closure s "
            "WHERE a.descendant_id = CAST(:pid AS uuid) AND s.ancestor_id = CAST(:oid AS uuid)"
        ),
        {"pid": new_parent_id, "oid": org_id, "cid": parent.company_id},
    )
    await conn.execute(
        text(
            "UPDATE iam.org_unit SET path = CAST(:pp AS ltree) || subpath(path, nlevel(CAST(:op AS ltree)) - 1), "
            "updated_at = now() WHERE path <@ CAST(:op AS ltree)"
        ),
        {"pp": parent.path, "op": node.path},
    )
    await conn.execute(update(t.org_unit).where(t.org_unit.c.id == org_id).values(parent_id=new_parent_id))
    if cross_company:
        cid = parent.company_id
        await conn.execute(update(t.org_unit).where(t.org_unit.c.id.in_(ids)).values(company_id=cid))
        await conn.execute(update(t.org_closure).where(t.org_closure.c.descendant_id.in_(ids)).values(company_id=cid))
        await conn.execute(update(t.iam_user).where(t.iam_user.c.home_org_id.in_(ids)).values(company_id=cid))
        await conn.execute(
            update(t.user_org_membership).where(t.user_org_membership.c.org_id.in_(ids)).values(company_id=cid)
        )
        await conn.execute(update(t.user_role).where(t.user_role.c.scope_org_id.in_(ids)).values(company_id=cid))
    await emit(
        conn,
        "org.moved",
        parent.company_id,
        org_id,
        actor.user_id,
        {"from_parent": str(node.parent_id), "to_parent": str(new_parent_id)},
    )
    return await get_org(conn, org_id)


async def disable_org(conn: AsyncConnection, actor: Actor, org_id: uuid.UUID) -> Org:
    node = await get_org(conn, org_id)
    if node.type == "group":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "集团根不能停用")
    actor.require(actor.has("platform_admin") if node.type == "company" else actor.manages_org(org_id))
    ids = list(await descendants(conn, org_id))
    await conn.execute(
        update(t.org_unit)
        .where(and_(t.org_unit.c.id.in_(ids), t.org_unit.c.status == "active"))
        .values(status="disabled", valid_to=utcnow(), updated_at=utcnow())
    )
    await emit(conn, "org.disabled", node.company_id, org_id, actor.user_id, {"subtree_size": len(ids)})
    return await get_org(conn, org_id)
