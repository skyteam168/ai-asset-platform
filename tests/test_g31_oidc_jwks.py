"""G3.1：非 stub OIDC — JWKS 校验 id_token，仅映射 sub。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from aip.control_plane.iam.oidc_verify import verify_id_token_claims, verify_oidc_id_token
from aip.control_plane.iam.providers import OidcStubAuthProvider
from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError

ISSUER = "http://127.0.0.1/realms/aip"
AUD = "aip-console"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _rsa_jwk_pair() -> tuple[Any, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    import base64

    def b64u(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "kid": "g31-test", "use": "sig", "alg": "RS256", "n": b64u(pub.n), "e": b64u(pub.e)}
    return key, jwk


def _mint(priv: Any, *, sub: str, aud: str = AUD, iss: str = ISSUER, roles: list[str] | None = None) -> str:
    now = dt.datetime.now(dt.UTC)
    claims: dict[str, Any] = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
    }
    if roles is not None:
        claims["realm_access"] = {"roles": roles}
        claims["org"] = "evil-external-org"
    return jwt.encode(claims, priv, algorithm="RS256", headers={"kid": "g31-test"})


@pytest.fixture
async def jwks_server() -> AsyncIterator[tuple[str, Any, dict[str, Any]]]:
    priv, jwk = _rsa_jwk_pair()

    async def handle(request: Request) -> JSONResponse:
        return JSONResponse({"keys": [jwk]})

    app = Starlette(routes=[Route("/certs", handle, methods=["GET"])])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}/certs", priv, jwk
    server.should_exit = True
    await task


def _settings(jwks_url: str) -> Settings:
    return Settings(
        env="test",
        oidc_enabled=True,
        oidc_stub=False,
        oidc_issuer=ISSUER,
        oidc_client_id=AUD,
        oidc_audience=AUD,
        oidc_jwks_url=jwks_url,
    )


def test_verify_ignores_external_roles() -> None:
    priv, jwk = _rsa_jwk_pair()
    tok = _mint(priv, sub="kc-user", roles=["platform_admin"])
    s = _settings("http://unused")
    sub = verify_id_token_claims(tok, settings=s, jwks={"keys": [jwk]})
    assert sub == "kc-user"


async def test_oidc_provider_fetches_jwks_and_maps_sub(jwks_server: tuple[str, Any, dict[str, Any]]) -> None:
    url, priv, _jwk = jwks_server
    s = _settings(url)
    tok = _mint(priv, sub="kc-vn-1", roles=["admin"])
    p = OidcStubAuthProvider()
    ident = await p.authenticate({"id_token": tok}, s)
    assert ident.provider == "oidc"
    assert ident.external_id == "kc-vn-1"


async def test_oidc_rejects_wrong_audience(jwks_server: tuple[str, Any, dict[str, Any]]) -> None:
    url, priv, _jwk = jwks_server
    s = _settings(url)
    tok = _mint(priv, sub="x", aud="other-client")
    with pytest.raises(PlatformError) as ei:
        await verify_oidc_id_token(tok, s)
    assert ei.value.code == ErrorCode.UNAUTHENTICATED


async def test_prod_settings_reject_oidc_stub() -> None:
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
            oidc_enabled=True,
            oidc_stub=True,
        )
