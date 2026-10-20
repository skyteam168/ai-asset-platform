"""消费方、凭据、断言验签公钥、用户绑定（architecture.md 3.1、10.1）。"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from jwt import PyJWK
from jwt.exceptions import PyJWKError
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.control_plane.iam.events import emit
from aip.control_plane.iam.orgs import get_org
from aip.control_plane.iam.users import user_row
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.tokens.secrets_util import new_opaque_token

DEFAULT_TOKEN_DAYS = 90
DEFAULT_BINDING_DAYS = 365
BIND_POLICIES = frozenset({"strict", "by_employee_no", "app_only"})


async def consumer_row(conn: AsyncConnection, consumer_id: uuid.UUID) -> dict[str, Any]:
    row = (await conn.execute(select(t.consumer).where(t.consumer.c.id == consumer_id))).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "消费方不存在或不可见")
    return dict(row)


async def create_consumer(
    conn: AsyncConnection,
    actor: Actor,
    *,
    client_id: str,
    company_id: uuid.UUID,
    clearance_cap: int,
    bind_policy: str = "strict",
    contact: str | None = None,
) -> uuid.UUID:
    """消费方由总部审批创建（architecture.md 10.1）；by_employee_no 需双人复核，M1 先限定平台管理员。"""
    actor.require(actor.has("platform_admin"), "只有平台管理员可以注册消费方")
    if bind_policy not in BIND_POLICIES or not 1 <= clearance_cap <= 4:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "bind_policy 或 clearance_cap 不合法")
    org = await get_org(conn, company_id)
    if org.type != "company":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "消费方必须归属公司节点")
    if (await conn.execute(select(t.consumer.c.id).where(t.consumer.c.client_id == client_id))).first():
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "client_id 已存在")
    cid = new_id()
    await conn.execute(
        t.consumer.insert().values(
            id=cid,
            client_id=client_id,
            company_id=company_id,
            clearance_cap=clearance_cap,
            bind_policy=bind_policy,
            status="active",
            contact=contact,
        )
    )
    await emit(conn, "consumer.created", company_id, cid, actor.user_id, {"client_id": client_id})
    return cid


QUOTA_KEYS = {"daily_calls", "daily_writes"}


def validate_quota(quota: dict[str, Any], allowed: set[str]) -> dict[str, int]:
    """配额：键必须是已知项、值为正整数；空对象表示不限。"""
    bad = set(quota) - allowed
    if bad:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"未知的配额项：{', '.join(sorted(bad))}")
    out: dict[str, int] = {}
    for k, v in quota.items():
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"配额 {k} 必须是正整数")
        out[k] = v
    return out


async def set_quota(
    conn: AsyncConnection, actor: Actor, consumer_id: uuid.UUID, quota: dict[str, Any]
) -> dict[str, int]:
    actor.require(actor.has("platform_admin"), "消费方配额由平台管理员设定")
    c = await consumer_row(conn, consumer_id)
    clean = validate_quota(quota, QUOTA_KEYS)
    await conn.execute(update(t.consumer).where(t.consumer.c.id == consumer_id).values(quota=clean))
    await emit(conn, "consumer.quota_set", c["company_id"], consumer_id, actor.user_id, clean)
    return clean


async def issue_static_token(
    conn: AsyncConnection,
    actor: Actor,
    consumer_id: uuid.UUID,
    days: int = DEFAULT_TOKEN_DAYS,
    ip_binding: list[str] | None = None,
) -> tuple[uuid.UUID, str, dt.datetime]:
    actor.require(actor.has("platform_admin"))
    c = await consumer_row(conn, consumer_id)
    token, digest = new_opaque_token("aipk")
    cred_id = new_id()
    expires = utcnow() + dt.timedelta(days=days)
    await conn.execute(
        t.consumer_credential.insert().values(
            id=cred_id,
            consumer_id=consumer_id,
            company_id=c["company_id"],
            kind="static_token",
            token_hash=digest,
            ip_binding=ip_binding,
            expires_at=expires,
            status="active",
        )
    )
    await emit(conn, "credential.issued", c["company_id"], cred_id, actor.user_id)
    return cred_id, token, expires


async def revoke_credential(conn: AsyncConnection, actor: Actor, credential_id: uuid.UUID) -> None:
    actor.require(actor.has("platform_admin"))
    row = (
        await conn.execute(
            select(t.consumer_credential.c.company_id).where(t.consumer_credential.c.id == credential_id)
        )
    ).first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "凭据不存在")
    await conn.execute(
        update(t.consumer_credential).where(t.consumer_credential.c.id == credential_id).values(status="revoked")
    )
    await emit(conn, "credential.revoked", row[0], credential_id, actor.user_id)


async def add_jwk(conn: AsyncConnection, actor: Actor, consumer_id: uuid.UUID, jwk: dict[str, Any]) -> str:
    """登记用户断言验签公钥；只接受公钥（不允许带私钥参数 d）。"""
    actor.require(actor.has("platform_admin"))
    c = await consumer_row(conn, consumer_id)
    if "d" in jwk or not jwk.get("kid"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "只能登记带 kid 的公钥")
    alg = "ES256" if jwk.get("kty") == "EC" else "RS256"
    try:
        PyJWK.from_json(json.dumps(jwk), algorithm=alg)
    except (PyJWKError, ValueError) as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "JWK 无法解析") from e
    await conn.execute(
        t.consumer_jwk.insert().values(
            consumer_id=consumer_id, kid=jwk["kid"], company_id=c["company_id"], jwk=jwk, status="active"
        )
    )
    await emit(conn, "jwk.added", c["company_id"], consumer_id, actor.user_id, {"kid": jwk["kid"]})
    return str(jwk["kid"])


async def revoke_jwk(conn: AsyncConnection, actor: Actor, consumer_id: uuid.UUID, kid: str) -> None:
    actor.require(actor.has("platform_admin"))
    c = await consumer_row(conn, consumer_id)
    await conn.execute(
        update(t.consumer_jwk)
        .where(and_(t.consumer_jwk.c.consumer_id == consumer_id, t.consumer_jwk.c.kid == kid))
        .values(status="revoked")
    )
    await emit(conn, "jwk.revoked", c["company_id"], consumer_id, actor.user_id, {"kid": kid})


async def create_binding(
    conn: AsyncConnection,
    actor: Actor,
    consumer_id: uuid.UUID,
    external_subject: str,
    user_id: uuid.UUID,
    days: int = DEFAULT_BINDING_DAYS,
) -> uuid.UUID:
    """分公司管理员维护本公司消费方的绑定；被绑定用户必须在其管理子树内。"""
    c = await consumer_row(conn, consumer_id)
    u = await user_row(conn, user_id)
    actor.require(
        actor.manages_org(c["company_id"]) and actor.manages_org(u["home_org_id"]),
        "只能为本公司消费方绑定本公司子树内的用户",
    )
    bid = new_id()
    await conn.execute(
        t.user_binding.insert().values(
            id=bid,
            consumer_id=consumer_id,
            external_subject=external_subject,
            user_id=user_id,
            company_id=c["company_id"],
            created_by=actor.user_id,
            expires_at=utcnow() + dt.timedelta(days=days),
        )
    )
    await emit(conn, "binding.created", c["company_id"], bid, actor.user_id)
    return bid
