"""H44：服务间短期 JWT + 可选 mTLS 客户端证书校验。"""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.serviceauth import (
    bearer_for_outbound,
    mint_service_token,
    require_service_auth,
    verify_client_cert_pem,
    verify_service_jwt,
)
from aip.libs.tokens.keys import SigningKey


def _pem() -> str:
    key = SigningKey.generate()
    assert key.private is not None
    return key.private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _s(**kwargs: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "issuer": "https://api.aip.internal",
        "service_auth_mode": "both",
        "internal_service_token": "static-xyz",
        "service_token_ttl_s": 300,
        "signing_key_pem": _pem(),
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


def test_mint_and_verify_service_jwt() -> None:
    s = _s()
    tok = mint_service_token(subject="control-plane", settings=s)
    assert verify_service_jwt(tok, settings=s) == "control-plane"


def test_require_accepts_static_and_jwt() -> None:
    s = _s()
    assert require_service_auth(f"Bearer {s.internal_service_token}", settings=s) == "static"
    assert require_service_auth(f"Bearer {mint_service_token(settings=s)}", settings=s) == "control-plane"


def test_jwt_only_rejects_static() -> None:
    s = _s(service_auth_mode="jwt")
    with pytest.raises(PlatformError) as ei:
        require_service_auth(f"Bearer {s.internal_service_token}", settings=s)
    assert ei.value.code == ErrorCode.UNAUTHENTICATED


def test_expired_jwt_rejected() -> None:
    s = _s(service_auth_mode="jwt", service_token_ttl_s=60)
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    tok = mint_service_token(settings=s, now=past)
    with pytest.raises(PlatformError) as ei:
        verify_service_jwt(tok, settings=s)
    assert ei.value.code == ErrorCode.TOKEN_EXPIRED


def _ca_and_client() -> tuple[str, str]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aip-service-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "data-plane")])
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(client_name)
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=30))
        .sign(ca_key, hashes.SHA256())
    )
    return (
        ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
        client_cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


def test_mtls_client_cert_required_when_ca_set() -> None:
    ca_pem, client_pem = _ca_and_client()
    assert verify_client_cert_pem(client_pem, ca_pem)
    s = _s(service_auth_mode="static", service_mtls_ca_pem=ca_pem)
    with pytest.raises(PlatformError):
        require_service_auth(f"Bearer {s.internal_service_token}", settings=s)
    assert (
        require_service_auth(f"Bearer {s.internal_service_token}", settings=s, client_cert_pem=client_pem) == "static"
    )


def test_bearer_for_outbound_jwt_mode() -> None:
    s = _s(service_auth_mode="both")
    tok = bearer_for_outbound(settings=s)
    assert verify_service_jwt(tok, settings=s) == "control-plane"


def test_bearer_for_outbound_both_without_shared_key_uses_static() -> None:
    s = Settings(
        env="test",
        issuer="https://api.aip.internal",
        service_auth_mode="both",
        internal_service_token="static-xyz",
        service_token_ttl_s=300,
    )
    assert bearer_for_outbound(settings=s) == "static-xyz"
