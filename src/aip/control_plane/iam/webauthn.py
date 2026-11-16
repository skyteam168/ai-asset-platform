"""WebAuthn 可选第二因子（architecture.md 3.4）。

stub：跳过 attestation/assertion 签名，仅校验挑战与 sign_count（本地测例）。
非 stub：校验 clientDataJSON 挑战/origin，并用 SPKI 公钥校验 assertion（ES256）。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
import struct
import uuid
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam import tables as t
from aip.control_plane.iam.authz import Actor
from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except Exception as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "base64url 无效") from e


def _allowed_origins(settings: Settings) -> set[str]:
    raw = (settings.webauthn_origins or "").strip()
    if raw:
        return {o.strip() for o in raw.split(",") if o.strip()}
    # 默认：本地控制台 + 与 rp_id 匹配的 https
    rp = settings.webauthn_rp_id or "localhost"
    return {
        "http://localhost:18081",
        "http://127.0.0.1:18081",
        f"https://{rp}",
        f"http://{rp}",
        f"http://{rp}:18081",
    }


def _verify_client_data(
    client_data_json: bytes,
    *,
    expected_type: str,
    expected_challenge: str,
    settings: Settings,
) -> None:
    try:
        data = json.loads(client_data_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "clientDataJSON 无效") from e
    if not isinstance(data, dict):
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "clientDataJSON 无效")
    if data.get("type") != expected_type:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "clientDataJSON type 不匹配")
    chal = data.get("challenge")
    if not isinstance(chal, str) or chal != expected_challenge:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "clientDataJSON challenge 不匹配")
    origin = data.get("origin")
    allowed = _allowed_origins(settings)
    if not isinstance(origin, str) or origin not in allowed:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "clientDataJSON origin 不匹配")


def _sign_count_from_auth_data(authenticator_data: bytes) -> int:
    if len(authenticator_data) < 37:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "authenticatorData 过短")
    return int(struct.unpack(">I", authenticator_data[33:37])[0])


def _verify_assertion_signature(
    *,
    public_key_der: bytes,
    authenticator_data: bytes,
    client_data_json: bytes,
    signature: bytes,
) -> None:
    try:
        pub = load_der_public_key(public_key_der)
    except Exception as e:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "凭证公钥无效") from e
    if not isinstance(pub, ec.EllipticCurvePublicKey):
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "仅支持 ES256 凭证")
    signed = authenticator_data + hashlib.sha256(client_data_json).digest()
    try:
        pub.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as e:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "assertion 签名无效") from e


def make_test_es256_spki() -> tuple[bytes, ec.EllipticCurvePrivateKey]:
    """单测用：生成 ES256 密钥对，返回 (SPKI DER, private_key)。"""
    key = ec.generate_private_key(ec.SECP256R1())
    spki = key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return spki, key


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
        "stub": bool(s.webauthn_stub),
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
            "authenticatorSelection": {"userVerification": "preferred", "residentKey": "preferred"},
        },
    }


async def finish_registration(
    conn: AsyncConnection,
    actor: Actor,
    user_id: uuid.UUID,
    *,
    challenge_id: uuid.UUID,
    credential_id: str,
    public_key_cose_b64: str = "",
    public_key_spki_b64: str = "",
    client_data_json_b64: str = "",
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

    if s.webauthn_stub:
        raw_pk = public_key_spki_b64 or public_key_cose_b64
        if not raw_pk:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "缺少公钥材料")
        try:
            pk = _b64url_decode(raw_pk)
        except PlatformError:
            # stub 允许标准 base64
            try:
                pk = base64.urlsafe_b64decode(raw_pk + "=" * (-len(raw_pk) % 4))
            except Exception as e:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "公钥无效") from e
    else:
        if not public_key_spki_b64 or not client_data_json_b64 or not credential_id:
            raise PlatformError(
                ErrorCode.INVALID_ARGUMENT,
                "非 stub 须提供 credential_id、public_key_spki_b64、client_data_json_b64",
            )
        client_data = _b64url_decode(client_data_json_b64)
        _verify_client_data(
            client_data,
            expected_type="webauthn.create",
            expected_challenge=str(chal["challenge"]),
            settings=s,
        )
        pk = _b64url_decode(public_key_spki_b64)
        try:
            pub = load_der_public_key(pk)
        except Exception as e:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "public_key_spki 无效") from e
        if not isinstance(pub, ec.EllipticCurvePublicKey):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅支持 ES256 SPKI 公钥")

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
    """登录第二因子：按 login_name 发起挑战。"""
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
        "stub": bool(s.webauthn_stub),
        "publicKey": {
            "challenge": challenge,
            "timeout": s.webauthn_challenge_ttl_s * 1000,
            "rpId": s.webauthn_rp_id or "localhost",
            "allowCredentials": [{"type": "public-key", "id": c["credential_id"]} for c in creds],
            "userVerification": "preferred",
        },
    }


async def verify_authentication(
    conn: AsyncConnection,
    *,
    challenge_id: uuid.UUID,
    credential_id: str,
    sign_count: int | None = None,
    client_data_json_b64: str = "",
    authenticator_data_b64: str = "",
    signature_b64: str = "",
    settings: Settings | None = None,
) -> uuid.UUID:
    """校验认证挑战；非 stub 须校验 assertion 签名与 sign_count。"""
    s = settings or get_settings()
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
    stored = int(cred["sign_count"])

    if s.webauthn_stub:
        if sign_count is None:
            next_count = stored + 1
        else:
            if sign_count <= stored:
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "sign_count 重放或回退")
            next_count = sign_count
    else:
        if not client_data_json_b64 or not authenticator_data_b64 or not signature_b64:
            raise PlatformError(
                ErrorCode.INVALID_ARGUMENT,
                "非 stub 须提供 client_data_json_b64、authenticator_data_b64、signature_b64",
            )
        client_data = _b64url_decode(client_data_json_b64)
        auth_data = _b64url_decode(authenticator_data_b64)
        signature = _b64url_decode(signature_b64)
        _verify_client_data(
            client_data,
            expected_type="webauthn.get",
            expected_challenge=str(chal["challenge"]),
            settings=s,
        )
        _verify_assertion_signature(
            public_key_der=bytes(cred["public_key_cose"]),
            authenticator_data=auth_data,
            client_data_json=client_data,
            signature=signature,
        )
        next_count = _sign_count_from_auth_data(auth_data)
        if next_count <= stored:
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "sign_count 重放或回退")

    await conn.execute(
        update(t.webauthn_challenge).where(t.webauthn_challenge.c.id == challenge_id).values(consumed_at=now)
    )
    await conn.execute(
        update(t.webauthn_credential)
        .where(t.webauthn_credential.c.id == cred["id"])
        .values(sign_count=next_count, last_used_at=now)
    )
    return uuid.UUID(str(chal["user_id"]))


# 兼容旧测例名
async def stub_verify_authentication(
    conn: AsyncConnection,
    *,
    challenge_id: uuid.UUID,
    credential_id: str,
    sign_count: int | None = None,
    settings: Settings | None = None,
) -> uuid.UUID:
    return await verify_authentication(
        conn,
        challenge_id=challenge_id,
        credential_id=credential_id,
        sign_count=sign_count,
        settings=settings,
    )
