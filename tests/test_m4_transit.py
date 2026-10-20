"""M4：OpenBao Transit 托管平台签名密钥与机密加密（关闭 M1 / M2 延后项）。私钥不离开 OpenBao。"""

from __future__ import annotations

import secrets

import jwt
import pytest

from aip.libs.common import config
from aip.libs.tokens import platform_token, secrets_util
from aip.libs.tokens.keys import TransitClient, transit_keys
from aip.libs.tokens.signed import sign_payload, verify_payload

pytestmark = pytest.mark.infra
S = config.get_settings()


@pytest.fixture
def transit() -> tuple[TransitClient, str, str]:
    client = TransitClient(S.openbao_addr, S.openbao_token)
    sign_name, enc_name = f"aip-test-sign-{secrets.token_hex(3)}", f"aip-test-enc-{secrets.token_hex(3)}"
    client.ensure_key(sign_name, "ecdsa-p256")
    client.ensure_key(enc_name, "aes256-gcm96")
    return client, sign_name, enc_name


def test_transit_signed_access_token_and_snapshot(transit: tuple[TransitClient, str, str]) -> None:
    client, name, _ = transit
    keys = transit_keys(client, name)
    assert keys[0].private is None, "Transit 密钥在进程内没有私钥"
    tok, _ = platform_token.issue(
        keys[0],
        issuer="https://api",
        audience="https://mcp",
        kind="user",
        subject="u1",
        client_id="vn-ai",
        agent_profile=None,
        ttl_s=600,
    )
    claims = platform_token.verify(tok, keys=keys, issuer="https://api", audience="https://mcp")
    assert claims.subject == "u1"
    # 第三方用 JWKS 也能验签（标准 ES256 JWS）
    jwk = jwt.PyJWK(keys[0].public_jwk(), algorithm="ES256")
    assert jwt.decode(tok, key=jwk, algorithms=["ES256"], audience="https://mcp")["sub"] == "u1"
    payload = {"seq": 1, "users": {"u1": {"clearance": 2}}}
    digest, sig = sign_payload(payload, keys[0])
    assert verify_payload(payload, digest, sig, keys)


def test_key_rotation_keeps_old_tokens_valid(transit: tuple[TransitClient, str, str]) -> None:
    client, name, _ = transit
    before = transit_keys(client, name)
    old_token, _ = platform_token.issue(
        before[0], issuer="i", audience="a", kind="app", subject="c", client_id="c", agent_profile=None, ttl_s=600
    )
    client.rotate(name)
    after = transit_keys(client, name)
    assert len(after) == 2 and after[0].kid != before[0].kid
    assert platform_token.verify(old_token, keys=after, issuer="i", audience="a").subject == "c"
    with pytest.raises(RuntimeError, match="最新版本"):
        after[1].sign(b"x")  # 旧版本只能验签
    new_token, _ = platform_token.issue(
        after[0], issuer="i", audience="a", kind="app", subject="c", client_id="c", agent_profile=None, ttl_s=600
    )
    assert jwt.get_unverified_header(new_token)["kid"] == after[0].kid


def test_totp_seed_encryption_via_transit(
    transit: tuple[TransitClient, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, enc = transit
    legacy = secrets_util.seal("JBSWY3DPEHPK3PXP")  # 迁移前的本地密文
    monkeypatch.setenv("AIP_SECRET_BACKEND", "transit")
    monkeypatch.setenv("AIP_TRANSIT_SECRET_KEY", enc)
    config.get_settings.cache_clear()
    try:
        sealed = secrets_util.seal("JBSWY3DPEHPK3PXP")
        assert sealed.startswith("vault:v1:")
        assert secrets_util.unseal(sealed) == "JBSWY3DPEHPK3PXP"
        assert secrets_util.unseal(legacy) == "JBSWY3DPEHPK3PXP", "旧密文可继续解密，逐步迁移"
    finally:
        config.get_settings.cache_clear()


def test_prod_settings_accept_transit_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "AIP_ENV": "prod",
        "AIP_SIGNING_BACKEND": "transit",
        "AIP_SECRET_BACKEND": "transit",
        "AIP_OPENBAO_TOKEN": "prod-token",
        "AIP_INTERNAL_SERVICE_TOKEN": "prod-internal",
        "AIP_DATABASE_URL": "postgresql+asyncpg://aip:x@db/aip",
        "AIP_APP_DATABASE_URL": "postgresql+asyncpg://aip_app:x@db/aip",
        "AIP_SECRET_BOX_KEY": "cHJvZC1rZXktcHJvZC1rZXktcHJvZC1rZXktcHJvZDE=",
    }.items():
        monkeypatch.setenv(k, v)
    s = config.Settings()
    assert s.signing_backend == "transit"
    monkeypatch.setenv("AIP_SIGNING_BACKEND", "local")
    with pytest.raises(ValueError, match="signing_key_pem"):
        config.Settings()
