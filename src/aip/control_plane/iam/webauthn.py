"""WebAuthn 可选第二因子骨架（architecture.md 3.4）。

真密钥器联调属 Wave B；stub 模式用固定 attestation 完成注册/校验测例，不引入 fido2 依赖。
"""

from __future__ import annotations

import base64
import datetime as dt
import secrets
import uuid
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


async def begin_registration(
    conn: AsyncConnection,
    actor: Actor,
    user_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    if not s.webauthn_enabled:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "WebAuthn 未启用")
    actor.require(actor.user_id == user_id or actor.has("platform_admin"), "只能为自己注册安全密钥")
    u = (await conn.execute(select(t.iam_user).where(t.iam_user.c.id == user_id))).mappings().first()
    if u is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "用户不存在")
    challenge = _b64url(secrets.token_bytes(32))
    cid = new_id()
    now = utcnow()
    await conn.execute(
        t.webauthn_challenge.insert().values(
            id=cid,
            user_id=user_id,
            company_id=u["company_id"],
            kind="register",
            challenge=challenge,
            expires_at=now + dt.timedelta(seconds=s.webauthn_challenge_ttl_s),
            created_at=now,
        )
    )
    return {
        "challenge_id": str(cid),
        "publicKey": {
            "challenge": challenge,
            "rp": {"id": s.webauthn_rp_id or "localhost", "name": s.webauthn_rp_name},
            "user": {
                "id": _b64url(user_id.bytes),
                "name": u["login_name"] or str(user_id),
                "displayName": u["display_name"],
            },
            "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
            "timeout": s.webauthn_challenge_ttl_s * 1000,
            "attestation": "none",
        },
    }


async def finish_registration(
    conn: AsyncConnection,
    actor: Actor,
    user_id: uuid.UUID,
    *,
    challenge_id: uuid.UUID,
    credential_id: str,
    public_key_cose_b64: str,
    nickname: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    if not s.webauthn_enabled:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "WebAuthn 未启用")
    actor.require(actor.user_id == user_id or actor.has("platform_admin"), "只能为自己注册安全密钥")
    chal = (
        await conn.execute(select(t.webauthn_challenge).where(t.webauthn_challenge.c.id == challenge_id))
    ).mappings().first()
    if chal is None or chal["kind"] != "register" or chal["user_id"] != user_id:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "挑战无效")
    now = utcnow()
    if chal["consumed_at"] is not None or chal["expires_at"] <= now:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "挑战已过期或已使用")
    if not s.webauthn_stub:
        raise PlatformError(
            ErrorCode.PRECONDITION_FAILED,
            "非 stub 模式需校验 attestation；本地请设 AIP_WEBAUTHN_STUB=true",
        )
    try:
        pk = base64.urlsafe_b64decode(public_key_cose_b64 + "=" * (-len(public_key_cose_b64) % 4))
    except Exception as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "public_key_cose 无效") from e
    if not credential_id or not pk:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "缺少凭证材料")
    await conn.execute(
        update(t.webauthn_challenge)
        .where(t.webauthn_challenge.c.id == challenge_id)
        .values(consumed_at=now)
    )
    u = (await conn.execute(select(t.iam_user).where(t.iam_user.c.id == user_id))).mappings().first()
    assert u is not None
    rid = new_id()
    await conn.execute(
        t.webauthn_credential.insert().values(
            id=rid,
            user_id=user_id,
            company_id=u["company_id"],
            credential_id=credential_id,
            public_key_cose=pk,
            sign_count=0,
            transports=[],
            nickname=nickname or "",
            created_at=now,
        )
    )
    return {"id": str(rid), "credential_id": credential_id, "nickname": nickname or ""}


async def list_credentials(conn: AsyncConnection, user_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await conn.execute(select(t.webauthn_credential).where(t.webauthn_credential.c.user_id == user_id).limit(50))
    ).mappings()
    return [
        {
            "id": str(r["id"]),
            "credential_id": r["credential_id"],
            "nickname": r["nickname"],
            "sign_count": int(r["sign_count"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


async def begin_authentication(
    conn: AsyncConnection,
    login_name: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """登录第二因子：按 login_name 发起挑战（stub；真机需 allowCredentials）。"""
    s = settings or get_settings()
    if not s.webauthn_enabled:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "WebAuthn 未启用")
    u = (await conn.execute(select(t.iam_user).where(t.iam_user.c.login_name == login_name))).mappings().first()
    if u is None or u["status"] != "active":
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "用户名、口令或动态码错误")
    creds = await list_credentials(conn, u["id"])
    if not creds:
        raise PlatformError(ErrorCode.MFA_REQUIRED, "未登记安全密钥")
    challenge = _b64url(secrets.token_bytes(32))
    cid = new_id()
    now = utcnow()
    await conn.execute(
        t.webauthn_challenge.insert().values(
            id=cid,
            user_id=u["id"],
            company_id=u["company_id"],
            kind="authenticate",
            challenge=challenge,
            expires_at=now + dt.timedelta(seconds=s.webauthn_challenge_ttl_s),
            created_at=now,
        )
    )
    return {
        "challenge_id": str(cid),
        "publicKey": {
            "challenge": challenge,
            "timeout": s.webauthn_challenge_ttl_s * 1000,
            "rpId": s.webauthn_rp_id or "localhost",
            "allowCredentials": [{"type": "public-key", "id": c["credential_id"]} for c in creds],
            "userVerification": "preferred",
        },
    }


async def stub_verify_authentication(
    conn: AsyncConnection,
    *,
    challenge_id: uuid.UUID,
    credential_id: str,
    settings: Settings | None = None,
) -> uuid.UUID:
    """stub：确认挑战未过期且 credential 属于该用户，返回 user_id。"""
    s = settings or get_settings()
    if not s.webauthn_stub:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "非 stub 需校验 assertion 签名")
    chal = (
        await conn.execute(select(t.webauthn_challenge).where(t.webauthn_challenge.c.id == challenge_id))
    ).mappings().first()
    now = utcnow()
    if chal is None or chal["kind"] != "authenticate" or chal["consumed_at"] or chal["expires_at"] <= now:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "挑战无效")
    cred = (
        await conn.execute(
            select(t.webauthn_credential).where(
                and_(
                    t.webauthn_credential.c.credential_id == credential_id,
                    t.webauthn_credential.c.user_id == chal["user_id"],
                )
            )
        )
    ).mappings().first()
    if cred is None:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "凭证未知")
    await conn.execute(
        update(t.webauthn_challenge).where(t.webauthn_challenge.c.id == challenge_id).values(consumed_at=now)
    )
    await conn.execute(
        update(t.webauthn_credential)
        .where(t.webauthn_credential.c.id == cred["id"])
        .values(sign_count=int(cred["sign_count"]) + 1, last_used_at=now)
    )
    return uuid.UUID(str(chal["user_id"]))
