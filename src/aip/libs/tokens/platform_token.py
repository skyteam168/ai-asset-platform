"""平台访问令牌（architecture.md 3.1 模式 A）：只含认证信息，不含组织、角色、密级。"""

from __future__ import annotations

import base64
import datetime as dt
import json
import secrets
from dataclasses import dataclass
from typing import Any, Literal

import jwt
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import SigningKey

# 平台令牌允许出现的全部 claim；测试断言令牌里没有其他字段
ALLOWED_CLAIMS = frozenset({"iss", "sub", "aud", "iat", "exp", "jti", "act", "kind", "agent_profile"})


@dataclass(frozen=True)
class AccessClaims:
    kind: Literal["user", "app"]
    subject: str  # user：内部 user_id；app：client_id
    client_id: str
    agent_profile: str | None
    jti: str
    expires_at: dt.datetime


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jws_es256(claims: dict[str, Any], key: SigningKey, headers: dict[str, str]) -> str:
    """手工组装 JWS：签名来自 key.sign()（本地或 Transit），DER 转为 ES256 要求的 r||s 定长格式。"""
    header = {"alg": "ES256", **headers}
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    )
    r, s = decode_dss_signature(key.sign(signing_input.encode()))
    return f"{signing_input}.{_b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def issue(
    key: SigningKey,
    *,
    issuer: str,
    audience: str,
    kind: Literal["user", "app"],
    subject: str,
    client_id: str,
    agent_profile: str | None,
    ttl_s: int,
    now: dt.datetime | None = None,
) -> tuple[str, int]:
    now = now or dt.datetime.now(dt.UTC)
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + ttl_s,
        "jti": secrets.token_urlsafe(16),
        "act": {"sub": client_id},
        "kind": kind,
    }
    if agent_profile:
        claims["agent_profile"] = agent_profile
    return jws_es256(claims, key, {"kid": key.kid, "typ": "at+jwt"}), ttl_s


def verify(token: str, *, keys: list[SigningKey], issuer: str, audience: str) -> AccessClaims:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        key = next((k for k in keys if k.kid == kid), None)
        if key is None:
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "令牌签名密钥未知")
        c: dict[str, Any] = jwt.decode(
            token,
            key=key.public_key,
            algorithms=["ES256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["sub", "exp", "iat", "jti", "act", "kind"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise PlatformError(ErrorCode.TOKEN_EXPIRED, "访问令牌已过期") from e
    except jwt.PyJWTError as e:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "访问令牌无效") from e
    if c["kind"] not in ("user", "app") or not isinstance(c.get("act"), dict):
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "访问令牌无效")
    return AccessClaims(
        kind=c["kind"],
        subject=c["sub"],
        client_id=str(c["act"].get("sub", "")),
        agent_profile=c.get("agent_profile"),
        jti=c["jti"],
        expires_at=dt.datetime.fromtimestamp(c["exp"], dt.UTC),
    )
