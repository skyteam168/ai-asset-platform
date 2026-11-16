"""P8：非 stub WebAuthn 注册/认证 API（合成 ES256 assertion）。"""

from __future__ import annotations

import hashlib
import json
import struct

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from aip.control_plane.app import create_app
from aip.control_plane.db import Database
from aip.control_plane.iam.webauthn import _b64url, make_test_es256_spki
from aip.libs.common.config import get_settings
from iam_seed import Seed, app_url
from test_m1_api import login

pytestmark = pytest.mark.infra


@pytest.fixture
async def cp(db: Database, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.setenv("AIP_WEBAUTHN_ENABLED", "true")
    monkeypatch.setenv("AIP_WEBAUTHN_STUB", "false")
    monkeypatch.setenv("AIP_WEBAUTHN_ORIGINS", "http://localhost:18081")
    get_settings.cache_clear()
    app = create_app(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client
    get_settings.cache_clear()


async def test_register_and_authenticate_crypto(cp: httpx.AsyncClient, seeded: Seed) -> None:
    headers = await login(cp, seeded, "vn_admin")
    uid = str(seeded.users["vn_admin"])
    begin = await cp.post(f"/api/v1/users/{uid}/webauthn/register/begin", headers=headers)
    assert begin.status_code == 200, begin.text
    body = begin.json()
    assert body["stub"] is False
    challenge = body["publicKey"]["challenge"]
    challenge_id = body["challenge_id"]
    spki, priv = make_test_es256_spki()
    client_create = json.dumps(
        {
            "type": "webauthn.create",
            "challenge": challenge,
            "origin": "http://localhost:18081",
        }
    ).encode()
    cred_id = _b64url(b"cred-crypto-1")
    finish = await cp.post(
        f"/api/v1/users/{uid}/webauthn/register/finish",
        headers=headers,
        json={
            "challenge_id": challenge_id,
            "credential_id": cred_id,
            "public_key_spki_b64": _b64url(spki),
            "client_data_json_b64": _b64url(client_create),
            "nickname": "Yubi",
        },
    )
    assert finish.status_code == 201, finish.text

    ab = await cp.post("/api/v1/auth/webauthn/authenticate/begin", json={"login_name": "vn_admin"})
    assert ab.status_code == 200, ab.text
    achallenge = ab.json()["publicKey"]["challenge"]
    client_get = json.dumps(
        {
            "type": "webauthn.get",
            "challenge": achallenge,
            "origin": "http://localhost:18081",
        }
    ).encode()
    auth_data = b"\x00" * 32 + b"\x01" + struct.pack(">I", 3)
    signed = auth_data + hashlib.sha256(client_get).digest()
    sig = priv.sign(signed, ec.ECDSA(hashes.SHA256()))
    af = await cp.post(
        "/api/v1/auth/webauthn/authenticate/finish",
        json={
            "challenge_id": ab.json()["challenge_id"],
            "credential_id": cred_id,
            "client_data_json_b64": _b64url(client_get),
            "authenticator_data_b64": _b64url(auth_data),
            "signature_b64": _b64url(sig),
        },
    )
    assert af.status_code == 200, af.text
    assert af.json()["verified"] is True
    assert af.json()["user_id"] == uid
