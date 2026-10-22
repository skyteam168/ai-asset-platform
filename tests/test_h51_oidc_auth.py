"""H51：AuthProvider + OIDC stub + external_login 映射。"""

from __future__ import annotations

import json

import httpx
import pytest

from aip.control_plane.app import create_app
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import auth, external_login
from aip.control_plane.iam.providers import OidcStubAuthProvider, list_providers
from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import Seed, app_url
from test_m1_api import login

pytestmark = pytest.mark.infra


def _oidc_settings(**kwargs: object) -> Settings:
    base = get_settings().model_dump()
    base.update({"oidc_enabled": True, "oidc_stub": True, "oidc_issuer": "https://idp.example/realms/aip"})
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
async def cp(db: Database) -> httpx.AsyncClient:
    app = create_app(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client


async def test_list_providers_local_always_enabled() -> None:
    items = list_providers(Settings(oidc_enabled=False))
    by_name = {p["name"]: p["enabled"] for p in items}
    assert by_name["local"] is True
    assert by_name["oidc"] is False


async def test_oidc_stub_requires_binding(db: Database, seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIP_OIDC_ENABLED", "true")
    get_settings.cache_clear()
    s = _oidc_settings()
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as ei:
            await auth.login_via_provider(conn, "oidc", {"id_token": "stub:kc-user-1"}, settings=s)
        assert ei.value.code == ErrorCode.USER_NOT_BOUND
    get_settings.cache_clear()


async def test_oidc_stub_maps_external_login(db: Database, seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIP_OIDC_ENABLED", "true")
    get_settings.cache_clear()
    s = _oidc_settings()
    from iam_seed import actor_for

    admin = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        await external_login.bind(
            conn, admin, user_id=seeded.users["vn_admin"], provider="oidc", external_id="kc-vn-admin"
        )
        token, _expires, uid = await auth.login_via_provider(
            conn, "oidc", {"id_token": "stub:kc-vn-admin"}, settings=s
        )
        assert uid == seeded.users["vn_admin"]
        assert token.startswith("aips")
        payload = json.dumps({"sub": "kc-vn-admin", "iss": s.oidc_issuer, "aud": "aip-console"})
        s2 = _oidc_settings(oidc_audience="aip-console")
        token2, _, uid2 = await auth.login_via_provider(conn, "oidc", {"id_token": payload}, settings=s2)
        assert uid2 == seeded.users["vn_admin"]
        assert token2.startswith("aips")
    get_settings.cache_clear()


async def test_oidc_stub_rejects_wrong_aud() -> None:
    p = OidcStubAuthProvider()
    s = _oidc_settings(oidc_audience="expected")
    with pytest.raises(PlatformError) as ei:
        await p.authenticate({"id_token": json.dumps({"sub": "x", "aud": "other"})}, s)
    assert ei.value.code == ErrorCode.UNAUTHENTICATED


async def test_bind_api_and_callback(cp: httpx.AsyncClient, seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIP_OIDC_ENABLED", "true")
    get_settings.cache_clear()
    headers = await login(cp, seeded, "admin")
    uid = str(seeded.users["vn_admin"])
    bound = await cp.post(
        f"/api/v1/users/{uid}/external-logins",
        headers=headers,
        json={"provider": "oidc", "external_id": "kc-api-1"},
    )
    assert bound.status_code == 201, bound.text
    providers = await cp.get("/api/v1/auth/providers")
    assert providers.status_code == 200
    names = {p["name"] for p in providers.json()["items"]}
    assert "local" in names and "oidc" in names
    cb = await cp.post("/api/v1/auth/oidc/callback", json={"id_token": "stub:kc-api-1"})
    assert cb.status_code == 200, cb.text
    assert cb.json()["user"]["login_name"] == "vn_admin"
    listed = await cp.get(f"/api/v1/users/{uid}/external-logins", headers=headers)
    assert listed.status_code == 200
    assert any(x["external_id"] == "kc-api-1" for x in listed.json()["items"])
    un = await cp.delete(f"/api/v1/users/{uid}/external-logins/oidc/kc-api-1", headers=headers)
    assert un.status_code == 204
    miss = await cp.post("/api/v1/auth/oidc/callback", json={"id_token": "stub:kc-api-1"})
    assert miss.status_code == 403
    assert miss.json()["code"] == "USER_NOT_BOUND"
    get_settings.cache_clear()


async def test_company_admin_cannot_bind_other_company(db: Database, seeded: Seed) -> None:
    from iam_seed import actor_for

    vn = await actor_for(db, seeded, "vn_admin")
    async with db.tx(vn.db_scope()) as conn:
        with pytest.raises(PlatformError) as ei:
            await external_login.bind(
                conn, vn, user_id=seeded.users["kh_admin"], provider="oidc", external_id="x"
            )
        # RLS 下他司用户不可见 → NOT_FOUND；若可见则 PERMISSION_DENIED
        assert ei.value.code in (ErrorCode.NOT_FOUND, ErrorCode.PERMISSION_DENIED)
