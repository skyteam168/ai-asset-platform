"""H52：authorization_code + PKCE 骨架。"""

from __future__ import annotations

import pytest

from aip.data_plane.pkce import new_code_verifier, s256_challenge, verify_pkce
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.replay import MemoryReplayStore
from iam_seed import Seed
from test_m1_oauth import PLATFORM, VN_KEY, Env, assertion

pytestmark = pytest.mark.infra


def test_s256_roundtrip() -> None:
    v = new_code_verifier()
    c = s256_challenge(v)
    verify_pkce(v, c, "S256")
    with pytest.raises(PlatformError) as ei:
        verify_pkce(v + "x", c, "S256")
    assert ei.value.code == ErrorCode.UNAUTHENTICATED


@pytest.fixture
async def env(db: object, seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("AIP_OAUTH_PKCE_ENABLED", "true")
    monkeypatch.setenv("AIP_OAUTH_PKCE_STUB", "true")
    get_settings.cache_clear()
    e = Env(db, seeded)  # type: ignore[arg-type]
    e.app = __import__("aip.data_plane.app", fromlist=["create_app"]).create_app(
        key=PLATFORM, replay=MemoryReplayStore()
    )
    import httpx

    e.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app), base_url="https://api.aip.internal")
    await e.setup()
    yield e
    await e.http.aclose()
    get_settings.cache_clear()


async def test_metadata_lists_authorization_code(env: Env) -> None:
    r = await env.http.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert "authorization_code" in body["grant_types_supported"]
    assert body["authorization_endpoint"].endswith("/oauth/authorize")
    assert "S256" in body["code_challenge_methods_supported"]


async def test_authorize_and_token_pkce(env: Env) -> None:
    verifier = new_code_verifier()
    challenge = s256_challenge(verifier)
    authz = await env.http.post(
        "/oauth/authorize",
        json={
            "client_id": "vn-ai",
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "subject": "vn.emp.001",
        },
    )
    assert authz.status_code == 200, authz.text
    code = authz.json()["code"]
    client_assertion = assertion(VN_KEY, "vn-ai", "vn-ai")
    tok = await env.http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_verifier": verifier,
            "client_id": "vn-ai",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
        },
    )
    assert tok.status_code == 200, tok.text
    access = tok.json()["access_token"]
    claims = __import__("aip.libs.tokens.platform_token", fromlist=["verify"]).verify(
        access, keys=[PLATFORM], issuer=get_settings().issuer, audience=get_settings().mcp_resource
    )
    assert claims.kind == "user"
    assert claims.subject == str(env.seed.users["vn_worker"])
    # 授权码一次性
    again = await env.http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_verifier": verifier,
            "client_id": "vn-ai",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
        },
    )
    assert again.status_code == 401


async def test_browser_consent_redirect_and_token(env: Env) -> None:
    """G3.2：GET 同意页 → POST consent → redirect code → token；缺 verifier / 重放失败。"""
    from urllib.parse import parse_qs, urlparse

    verifier = new_code_verifier()
    challenge = s256_challenge(verifier)
    page = await env.http.get(
        "/oauth/authorize",
        params={
            "client_id": "vn-ai",
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "subject": "vn.emp.001",
            "state": "st1",
        },
    )
    assert page.status_code == 200, page.text
    assert "授权第三方访问" in page.text
    consent = await env.http.post(
        "/oauth/authorize/consent",
        data={
            "client_id": "vn-ai",
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "subject": "vn.emp.001",
            "decision": "allow",
            "state": "st1",
        },
        follow_redirects=False,
    )
    assert consent.status_code == 302, consent.text
    loc = consent.headers["location"]
    assert "code=" in loc and "state=st1" in loc
    code = parse_qs(urlparse(loc).query)["code"][0]
    client_assertion = assertion(VN_KEY, "vn-ai", "vn-ai")
    missing = await env.http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "vn-ai",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
        },
    )
    assert missing.status_code == 400
    tok = await env.http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_verifier": verifier,
            "client_id": "vn-ai",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
        },
    )
    assert tok.status_code == 200, tok.text
    replay = await env.http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_verifier": verifier,
            "client_id": "vn-ai",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
        },
    )
    assert replay.status_code == 401


async def test_pkce_wrong_verifier(env: Env) -> None:
    verifier = new_code_verifier()
    authz = await env.http.post(
        "/oauth/authorize",
        json={
            "client_id": "vn-ai",
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_challenge": s256_challenge(verifier),
            "subject": "vn.emp.001",
        },
    )
    code = authz.json()["code"]
    client_assertion = assertion(VN_KEY, "vn-ai", "vn-ai")
    bad = await env.http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_verifier": new_code_verifier(),
            "client_id": "vn-ai",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
        },
    )
    assert bad.status_code == 401
