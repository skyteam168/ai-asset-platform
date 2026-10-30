"""服务间认证（architecture.md 3.3）：短期 JWT + 可选静态令牌回退 + 可选 mTLS 客户端证书。"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from typing import Any, Literal

import jwt
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import SigningKey, platform_keys
from aip.libs.tokens.platform_token import jws_es256

ServiceAuthMode = Literal["static", "jwt", "both"]
SERVICE_AUD_SUFFIX = "/internal"


def service_audience(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return f"{s.issuer.rstrip('/')}{SERVICE_AUD_SUFFIX}"


def _keys(settings: Settings) -> tuple[SigningKey, ...]:
    if settings.signing_key_pem:
        return (SigningKey.from_pem(settings.signing_key_pem),)
    return platform_keys()


def mint_service_token(
    *,
    subject: str = "control-plane",
    settings: Settings | None = None,
    now: dt.datetime | None = None,
) -> str:
    """签发短期服务 JWT（ES256，平台签名密钥）。"""
    s = settings or get_settings()
    now = now or dt.datetime.now(dt.UTC)
    key = _keys(s)[0]
    claims: dict[str, Any] = {
        "iss": s.issuer,
        "sub": subject,
        "aud": service_audience(s),
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + int(s.service_token_ttl_s),
        "jti": secrets.token_urlsafe(12),
        "kind": "service",
    }
    return jws_es256(claims, key, {"kid": key.kid, "typ": "service+jwt"})


def verify_service_jwt(token: str, *, settings: Settings | None = None) -> str:
    """校验服务 JWT，返回 sub。"""
    s = settings or get_settings()
    keys = _keys(s)
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        key = next((k for k in keys if k.kid == kid), None)
        if key is None:
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "服务令牌密钥未知")
        c: dict[str, Any] = jwt.decode(
            token,
            key=key.public_key,
            algorithms=["ES256"],
            audience=service_audience(s),
            issuer=s.issuer,
            options={"require": ["sub", "exp", "iat", "jti", "kind"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise PlatformError(ErrorCode.TOKEN_EXPIRED, "服务令牌已过期") from e
    except jwt.PyJWTError as e:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "服务令牌无效") from e
    if c.get("kind") != "service":
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "服务令牌无效")
    return str(c["sub"])


def verify_client_cert_pem(client_pem: str, ca_pem: str) -> bool:
    """用 CA 公钥校验客户端证书签名，且 issuer == CA.subject。"""
    try:
        cert = x509.load_pem_x509_certificate(client_pem.encode())
        ca = x509.load_pem_x509_certificate(ca_pem.encode())
        if cert.issuer != ca.subject:
            return False
        pub = ca.public_key()
        algo = cert.signature_hash_algorithm
        if algo is None:
            return False
        if isinstance(pub, rsa.RSAPublicKey):
            pub.verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), algo)
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(algo))
        else:
            return False
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def require_service_auth(
    authorization: str | None,
    *,
    settings: Settings | None = None,
    client_cert_pem: str | None = None,
) -> str:
    """校验 Authorization: Bearer …；返回主体（static 或 JWT sub）。"""
    s = settings or get_settings()
    mode: ServiceAuthMode = s.service_auth_mode  # type: ignore[assignment]
    given = ""
    if authorization and authorization.lower().startswith("bearer "):
        given = authorization[7:].strip()
    if not given:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "需要服务凭据")

    subject: str | None = None
    if mode in ("static", "both") and secrets.compare_digest(given, s.internal_service_token):
        subject = "static"
    if subject is None and mode in ("jwt", "both"):
        try:
            subject = verify_service_jwt(given, settings=s)
        except PlatformError:
            if mode == "jwt":
                raise
    if subject is None:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "需要服务凭据")

    ca = (s.service_mtls_ca_pem or "").strip()
    if ca:
        if not client_cert_pem or not verify_client_cert_pem(client_cert_pem, ca):
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "需要有效的服务客户端证书")
    return subject


def bearer_for_outbound(*, subject: str = "control-plane", settings: Settings | None = None) -> str:
    """控制面出站调用数据面时的 Bearer。

    - jwt：始终签发短期 JWT（要求两端共享 signing_key_pem / Transit）。
    - static：静态令牌。
    - both：已配置共享签名密钥时用 JWT；否则用静态令牌（开发多进程各自 platform_keys 无法互验）。
    """
    s = settings or get_settings()
    if s.service_auth_mode == "static":
        return s.internal_service_token
    if s.service_auth_mode == "jwt":
        return mint_service_token(subject=subject, settings=s)
    if s.signing_key_pem:
        return mint_service_token(subject=subject, settings=s)
    return s.internal_service_token


def cert_spki_sha256(pem: str) -> str:
    cert = x509.load_pem_x509_certificate(pem.encode())
    der = cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()
