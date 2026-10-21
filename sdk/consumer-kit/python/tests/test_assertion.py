"""签发自洽：本包签发 → PyJWT 验签校验 claims。"""

from __future__ import annotations

import jwt
import pytest

from aip_consumer_kit import generate_es256_keypair, sign_client_assertion, sign_user_assertion


def test_sign_user_assertion_roundtrip() -> None:
    pem, kid, pub_jwk = generate_es256_keypair()
    token = sign_user_assertion(
        pem,
        client_id="vn-ai",
        subject="emp-42",
        audience="https://aip.example/mcp",
        kid=kid,
        ttl_s=120,
    )
    claims = jwt.decode(
        token,
        jwt.algorithms.ECAlgorithm.from_jwk(pub_jwk),
        algorithms=["ES256"],
        audience="https://aip.example/mcp",
        options={"require": ["iss", "sub", "aud", "iat", "exp", "jti"]},
    )
    assert claims["iss"] == "vn-ai"
    assert claims["sub"] == "emp-42"
    assert claims["jti"]
    header = jwt.get_unverified_header(token)
    assert header["kid"] == kid
    assert header["alg"] == "ES256"


def test_sign_client_assertion_sub_equals_iss() -> None:
    pem, kid, pub_jwk = generate_es256_keypair()
    token = sign_client_assertion(
        pem,
        client_id="vn-ai",
        audience="https://aip.example/oauth/token",
        kid=kid,
    )
    claims = jwt.decode(
        token,
        jwt.algorithms.ECAlgorithm.from_jwk(pub_jwk),
        algorithms=["ES256"],
        audience="https://aip.example/oauth/token",
    )
    assert claims["iss"] == claims["sub"] == "vn-ai"


def test_ttl_cap() -> None:
    pem, kid, _ = generate_es256_keypair()
    with pytest.raises(ValueError, match="300"):
        sign_user_assertion(
            pem,
            client_id="vn-ai",
            subject="e",
            audience="aud",
            kid=kid,
            ttl_s=301,
        )


def test_jti_unique() -> None:
    pem, kid, _ = generate_es256_keypair()
    a = sign_user_assertion(pem, client_id="c", subject="s", audience="a", kid=kid)
    b = sign_user_assertion(pem, client_id="c", subject="s", audience="a", kid=kid)
    assert jwt.decode(a, options={"verify_signature": False})["jti"] != jwt.decode(
        b, options={"verify_signature": False}
    )["jti"]
