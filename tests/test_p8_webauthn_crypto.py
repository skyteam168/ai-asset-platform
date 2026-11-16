"""P8：WebAuthn 非 stub 路径（SPKI + assertion 签名）。"""

from __future__ import annotations

import base64
import hashlib
import json
import struct

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from aip.control_plane.iam.webauthn import (
    _b64url,
    _verify_assertion_signature,
    _verify_client_data,
    make_test_es256_spki,
)
from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError


def test_client_data_challenge_and_origin() -> None:
    s = Settings(webauthn_origins="https://console.example")
    chal = _b64url(b"\x01" * 32)
    body = json.dumps(
        {"type": "webauthn.create", "challenge": chal, "origin": "https://console.example"}
    ).encode()
    _verify_client_data(body, expected_type="webauthn.create", expected_challenge=chal, settings=s)
    with pytest.raises(PlatformError) as ei:
        _verify_client_data(
            body, expected_type="webauthn.get", expected_challenge=chal, settings=s
        )
    assert ei.value.code == ErrorCode.UNAUTHENTICATED


def test_assertion_signature_roundtrip() -> None:
    spki, priv = make_test_es256_spki()
    auth_data = b"\x00" * 32 + b"\x01" + struct.pack(">I", 7)
    client = b'{"type":"webauthn.get","challenge":"abc","origin":"http://localhost:18081"}'
    signed = auth_data + hashlib.sha256(client).digest()
    sig = priv.sign(signed, ec.ECDSA(hashes.SHA256()))
    _verify_assertion_signature(
        public_key_der=spki,
        authenticator_data=auth_data,
        client_data_json=client,
        signature=sig,
    )
    with pytest.raises(PlatformError):
        _verify_assertion_signature(
            public_key_der=spki,
            authenticator_data=auth_data,
            client_data_json=client,
            signature=sig[:-1] + bytes([sig[-1] ^ 0xFF]),
        )


def test_prod_rejects_webauthn_stub() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            env="prod",
            database_url="postgresql+asyncpg://owner:secret@db/aip",
            app_database_url="postgresql+asyncpg://aip_app:secret@db/aip",
            openbao_token="prod-openbao-token",
            secret_box_key="cHJvZC1vbmx5LXNlY3JldC1ib3gta2V5LTMyYnl0ZXMh",
            internal_service_token="prod-internal-service-token",
            signing_key_pem="-----BEGIN PRIVATE KEY-----\nMEw=\n-----END PRIVATE KEY-----",
            service_auth_mode="jwt",
            webauthn_enabled=True,
            webauthn_stub=True,
        )


def test_b64url_helpers_stable() -> None:
    raw = b"\xff\x00hello"
    enc = _b64url(raw)
    pad = "=" * (-len(enc) % 4)
    assert base64.urlsafe_b64decode(enc + pad) == raw
