"""external_login 绑定：OIDC/SAML 外部主体 → 内部 iam_user（architecture.md 3.3、11.1）。"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.events import emit
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow


async def resolve_user_id(conn: AsyncConnection, provider: str, external_id: str) -> uuid.UUID | None:
    row = (
        await conn.execute(
            select(t.external_login.c.user_id).where(
                and_(t.external_login.c.provider == provider, t.external_login.c.external_id == external_id)
            )
        )
    ).first()
    return uuid.UUID(str(row[0])) if row else None


async def bind(
    conn: AsyncConnection,
    actor: Actor,
    *,
    user_id: uuid.UUID,
    provider: str,
    external_id: str,
) -> dict[str, object]:
    if not provider or not external_id:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "provider 与 external_id 必填")
    u = (await conn.execute(select(t.iam_user).where(t.iam_user.c.id == user_id))).mappings().first()
    if u is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "用户不存在")
    actor.require(
        actor.has("platform_admin") or (actor.has("company_admin") and actor.manages_org(u["home_org_id"])),
        "仅平台管理员或本公司管理员可绑定外部登录",
    )
    existing = await resolve_user_id(conn, provider, external_id)
    if existing is not None and existing != user_id:
        raise PlatformError(ErrorCode.IDEMPOTENCY_CONFLICT, "该外部主体已绑定其他用户")
    if existing == user_id:
        return {
            "provider": provider,
            "external_id": external_id,
            "user_id": str(user_id),
            "company_id": str(u["company_id"]),
        }
    await conn.execute(
        t.external_login.insert().values(
            provider=provider,
            external_id=external_id,
            user_id=user_id,
            company_id=u["company_id"],
            created_at=utcnow(),
            created_by=actor.user_id,
        )
    )
    await emit(conn, "auth.external_login_bound", u["company_id"], user_id, actor.user_id)
    return {
        "provider": provider,
        "external_id": external_id,
        "user_id": str(user_id),
        "company_id": str(u["company_id"]),
    }


async def unbind(conn: AsyncConnection, actor: Actor, *, user_id: uuid.UUID, provider: str, external_id: str) -> None:
    u = (await conn.execute(select(t.iam_user).where(t.iam_user.c.id == user_id))).mappings().first()
    if u is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "用户不存在")
    actor.require(
        actor.has("platform_admin") or (actor.has("company_admin") and actor.manages_org(u["home_org_id"])),
        "仅平台管理员或本公司管理员可解绑外部登录",
    )
    res = await conn.execute(
        delete(t.external_login).where(
            and_(
                t.external_login.c.provider == provider,
                t.external_login.c.external_id == external_id,
                t.external_login.c.user_id == user_id,
            )
        )
    )
    if res.rowcount == 0:
        raise PlatformError(ErrorCode.NOT_FOUND, "外部登录绑定不存在")
    await emit(conn, "auth.external_login_unbound", u["company_id"], user_id, actor.user_id)


async def list_for_user(conn: AsyncConnection, user_id: uuid.UUID) -> list[dict[str, object]]:
    rows = (
        await conn.execute(select(t.external_login).where(t.external_login.c.user_id == user_id).limit(100))
    ).mappings()
    return [
        {
            "provider": r["provider"],
            "external_id": r["external_id"],
            "user_id": str(r["user_id"]),
            "company_id": str(r["company_id"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
