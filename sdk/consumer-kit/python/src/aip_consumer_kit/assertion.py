"""签发消费方用户断言 / 客户端断言（ES256，architecture.md 3.1）。"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def generate_es256_keypair() -> tuple[str, str, dict[str, str]]:
    """返回 (private_pem, kid, public_jwk)。"""
    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    from jwt.algorithms import ECAlgorithm

    jwk = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    kid = secrets.token_hex(8)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "ES256"
    return private_pem, kid, {k: str(v) for k, v in jwk.items()}


def sign_user_assertion(
    private_pem: str,
    *,
    client_id: str,
    subject: str,
    audience: str,
    kid: str,
    ttl_s: int = 120,
    extra: dict[str, Any] | None = None,
) -> str:
    """签发用户断言。每次调用新 jti；ttl_s 建议 ≤ 300。"""
    if ttl_s > 300:
        raise ValueError("断言有效期不得超过 300 秒（architecture.md 3.1）")
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": client_id,
        "sub": subject,
        "aud": audience,
        "iat": now,
        "exp": now + ttl_s,
        "jti": secrets.token_hex(8),
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, private_pem, algorithm="ES256", headers={"kid": kid})


def sign_client_assertion(
    private_pem: str,
    *,
    client_id: str,
    audience: str,
    kid: str,
    ttl_s: int = 120,
) -> str:
    """签发客户端断言：sub = iss = client_id。"""
    return sign_user_assertion(
        private_pem,
        client_id=client_id,
        subject=client_id,
        audience=audience,
        kid=kid,
        ttl_s=ttl_s,
    )
