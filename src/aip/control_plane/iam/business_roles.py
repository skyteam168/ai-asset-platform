"""公司业务角色：供 Workflow human_task 分派与待办匹配（非控制面 RBAC）。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.orgs import get_org
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
# 与 architecture.md 2.5 / users.ROLES 平台角色互斥，禁止注册为业务角色
PLATFORM_CODES = frozenset(
    {"platform_admin", "asset_developer", "data_owner", "business_owner", "company_admin", "auditor"}
)


def _out(row: Any) -> dict[str, Any]:
    return {
        "company_id": str(row["company_id"]),
        "code": row["code"],
        "name": row["name"],
        "description": row["description"] or "",
        "created_at": row["created_at"].isoformat().replace("+00:00", "Z") if row["created_at"] else None,
    }


async def list_roles(conn: AsyncConnection, company_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(
            select(t.business_role).where(t.business_role.c.company_id == company_id).order_by(t.business_role.c.code)
        )
    ).mappings().all()
    return [_out(r) for r in rows]


async def exists(conn: AsyncConnection, company_id: uuid.UUID, code: str) -> bool:
    return (
        await conn.execute(
            select(t.business_role.c.code).where(
                t.business_role.c.company_id == company_id, t.business_role.c.code == code
            )
        )
    ).first() is not None


async def create(
    conn: AsyncConnection,
    actor: Actor,
    company_id: uuid.UUID,
    code: str,
    name: str,
    description: str = "",
) -> dict[str, Any]:
    org = await get_org(conn, company_id)
    if org.type != "company":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "company_id 须为公司节点")
    actor.require(actor.has("platform_admin") or actor.manages_org(company_id), "无权配置该公司业务角色")
    if not CODE_RE.match(code):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "业务角色编码须为小写字母开头的 [a-z0-9_]，2–64 位")
    if code in PLATFORM_CODES:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "不能把平台角色注册为业务角色")
    if await exists(conn, company_id, code):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"业务角色 {code} 已存在")
    await conn.execute(
        t.business_role.insert().values(
            company_id=company_id, code=code, name=name, description=description, created_at=utcnow()
        )
    )
    row = (
        await conn.execute(
            select(t.business_role).where(
                t.business_role.c.company_id == company_id, t.business_role.c.code == code
            )
        )
    ).mappings().one()
    return _out(row)


async def delete(conn: AsyncConnection, actor: Actor, company_id: uuid.UUID, code: str) -> None:
    actor.require(actor.has("platform_admin") or actor.manages_org(company_id), "无权删除该公司业务角色")
    r = await conn.execute(
        t.business_role.delete().where(t.business_role.c.company_id == company_id, t.business_role.c.code == code)
    )
    if r.rowcount == 0:
        raise PlatformError(ErrorCode.NOT_FOUND, "业务角色不存在")
