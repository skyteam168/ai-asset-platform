"""H53：WebAuthn 注册/挑战骨架（stub）。"""

from __future__ import annotations

import base64

import httpx
import pytest

from aip.control_plane.app import create_app
from aip.control_plane.db import Database
from aip.libs.common.config import get_settings
from iam_seed import Seed, app_url
from test_m1_api import login

pytestmark = pytest.mark.infra


@pytest.fixture
async def cp(db: Database, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.setenv("AIP_WEBAUTHN_ENABLED", "true")
    monkeypatch.setenv("AIP_WEBAUTHN_STUB", "true")
    get_settings.cache_clear()
    app = create_app(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client
    get_settings.cache_clear()


async def test_register_and_authenticate_stub(cp: httpx.AsyncClient, seeded: Seed) -> None:
    headers = await login(cp, seeded, "vn_admin")
    uid = str(seeded.users["vn_admin"])
    begin = await cp.post(f"/api/v1/users/{uid}/webauthn/register/begin", headers=headers)
    assert begin.status_code == 200, begin.text
    challenge_id = begin.json()["challenge_id"]
    assert "publicKey" in begin.json()
    pk = base64.urlsafe_b64encode(b"\xa1\x01\x02cose-stub").decode().rstrip("=")
    finish = await cp.post(
        f"/api/v1/users/{uid}/webauthn/register/finish",
        headers=headers,
        json={
            "challenge_id": challenge_id,
            "credential_id": "cred-stub-1",
            "public_key_cose_b64": pk,
            "nickname": "YubiKey",
        },
    )
    assert finish.status_code == 201, finish.text
    listed = await cp.get(f"/api/v1/users/{uid}/webauthn/credentials", headers=headers)
    assert listed.status_code == 200
    assert any(c["credential_id"] == "cred-stub-1" for c in listed.json()["items"])
    ab = await cp.post("/api/v1/auth/webauthn/authenticate/begin", json={"login_name": "vn_admin"})
    assert ab.status_code == 200, ab.text
    af = await cp.post(
        "/api/v1/auth/webauthn/authenticate/finish",
        json={"challenge_id": ab.json()["challenge_id"], "credential_id": "cred-stub-1"},
    )
    assert af.status_code == 200, af.text
    assert af.json()["verified"] is True
    assert af.json()["user_id"] == uid
