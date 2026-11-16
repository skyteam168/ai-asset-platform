"""P8：OIDC authorization_code 换票拼装与解析。"""

from __future__ import annotations

from typing import Any

import pytest

from aip.control_plane.iam.oidc_verify import build_oidc_authorization_url, exchange_oidc_code
from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError


def test_build_authorization_url_pkce() -> None:
    s = Settings(
        oidc_authorization_url="https://idp.example/realms/aip/protocol/openid-connect/auth",
        oidc_client_id="aip-console",
        oidc_redirect_uri="https://console.example/login",
    )
    url = build_oidc_authorization_url(s, state="st1", code_challenge="chal")
    assert "client_id=aip-console" in url
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "state=st1" in url
    assert "redirect_uri=" in url


@pytest.mark.asyncio
async def test_exchange_oidc_code(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(
        oidc_token_url="https://idp.example/realms/aip/protocol/openid-connect/token",
        oidc_client_id="aip-console",
        oidc_client_secret="sec",
        oidc_redirect_uri="https://console.example/login",
    )

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"id_token": "eyJ.fake.token", "access_token": "x"}

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, path: str, data: dict[str, str]) -> _Resp:
            assert data["grant_type"] == "authorization_code"
            assert data["code"] == "abc"
            assert data["code_verifier"] == "ver"
            assert data["client_secret"] == "sec"
            return _Resp()

    monkeypatch.setattr(
        "aip.control_plane.iam.oidc_verify.build_client",
        lambda *a, **k: _Client(),
    )
    tok = await exchange_oidc_code(code="abc", code_verifier="ver", settings=s)
    assert tok.startswith("eyJ")


@pytest.mark.asyncio
async def test_exchange_requires_token_url() -> None:
    s = Settings(oidc_client_id="c", oidc_redirect_uri="https://x/login")
    with pytest.raises(PlatformError) as ei:
        await exchange_oidc_code(code="a", code_verifier="b", settings=s)
    assert ei.value.code == ErrorCode.PRECONDITION_FAILED
