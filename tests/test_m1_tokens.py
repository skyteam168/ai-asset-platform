"""M1：用户断言验签与平台令牌（纯单元测试，不需要基础设施）。"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import jwt
import pytest

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens import platform_token
from aip.libs.tokens.assertion import verify_assertion
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.replay import MemoryReplayStore

AUD = "https://api.aip.internal/oauth/token"
CONSUMER = SigningKey.generate()
OTHER = SigningKey.generate()
JWKS = {CONSUMER.kid: CONSUMER.public_jwk()}


def make(
    claims: dict[str, Any] | None = None,
    key: SigningKey = CONSUMER,
    kid: str | None = None,
    alg: str = "ES256",
    ttl: int = 120,
    now: dt.datetime | None = None,
) -> str:
    now = now or dt.datetime.now(dt.UTC)
    base: dict[str, Any] = {
        "iss": "vn-ai",
        "sub": "emp-001",
        "aud": AUD,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + ttl,
        "jti": secrets.token_hex(8),
    }
    base.update(claims or {})
    base = {k: v for k, v in base.items() if v is not None}
    return jwt.encode(base, key.private, algorithm=alg, headers={"kid": kid or key.kid})


async def check(token: str, store: MemoryReplayStore | None = None) -> Any:
    return await verify_assertion(
        token, client_id="vn-ai", jwks=JWKS, audiences=[AUD], replay=store or MemoryReplayStore()
    )


async def expect(code: ErrorCode, token: str, store: MemoryReplayStore | None = None) -> None:
    with pytest.raises(PlatformError) as e:
        await check(token, store)
    assert e.value.code == code


async def test_valid_assertion() -> None:
    v = await check(make({"act_agent": "production"}))
    assert (v.client_id, v.subject, v.act_agent) == ("vn-ai", "emp-001", "production")


async def test_forged_signature_rejected() -> None:
    await expect(ErrorCode.ASSERTION_INVALID, make(key=OTHER, kid=CONSUMER.kid))


async def test_unknown_kid_rejected() -> None:
    await expect(ErrorCode.ASSERTION_INVALID, make(key=OTHER))


async def test_expired_rejected() -> None:
    await expect(ErrorCode.ASSERTION_EXPIRED, make(now=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)))


async def test_lifetime_over_five_minutes_rejected() -> None:
    await expect(ErrorCode.ASSERTION_EXPIRED, make(ttl=600))


async def test_replay_rejected() -> None:
    store = MemoryReplayStore()
    token = make()
    await check(token, store)
    await expect(ErrorCode.ASSERTION_REPLAYED, token, store)


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "https://evil.example/token"},
        {"iss": "kh-ai"},
        {"jti": None},
        {"sub": None},
        {"iat": int(dt.datetime.now(dt.UTC).timestamp()) + 3600},
    ],
)
async def test_bad_claims_rejected(claims: dict[str, Any]) -> None:
    with pytest.raises(PlatformError) as e:
        await check(make(claims))
    assert e.value.code in (ErrorCode.ASSERTION_INVALID, ErrorCode.ASSERTION_EXPIRED)


async def test_hs256_and_none_algorithms_rejected() -> None:
    hs = jwt.encode(
        {"iss": "vn-ai", "sub": "x", "aud": AUD, "iat": 1, "exp": 2, "jti": "j"},
        "secret" * 6,
        algorithm="HS256",
        headers={"kid": CONSUMER.kid},
    )
    await expect(ErrorCode.ASSERTION_INVALID, hs)
    unsigned = jwt.encode({"iss": "vn-ai"}, None, algorithm="none")  # type: ignore[arg-type]
    await expect(ErrorCode.ASSERTION_INVALID, unsigned)


async def test_degraded_replay_store_only_accepts_short_lived() -> None:
    store = MemoryReplayStore()
    store.degraded = True
    await expect(ErrorCode.ASSERTION_EXPIRED, make(ttl=240), store)
    assert (await check(make(ttl=50), store)).subject == "emp-001"


def test_platform_token_has_only_auth_claims() -> None:
    key = SigningKey.generate()
    tok, _ = platform_token.issue(
        key,
        issuer="https://api",
        audience="https://mcp",
        kind="user",
        subject="u1",
        client_id="vn-ai",
        agent_profile="production",
        ttl_s=600,
    )
    claims = jwt.decode(tok, options={"verify_signature": False})
    assert set(claims) <= platform_token.ALLOWED_CLAIMS
    assert not {"roles", "org", "org_path", "clearance", "company_id", "data_scope"} & set(claims)
    assert claims["act"] == {"sub": "vn-ai"}
    parsed = platform_token.verify(tok, keys=[key], issuer="https://api", audience="https://mcp")
    assert (parsed.kind, parsed.subject, parsed.client_id) == ("user", "u1", "vn-ai")
    with pytest.raises(PlatformError):
        platform_token.verify(tok, keys=[key], issuer="https://api", audience="https://other")
    with pytest.raises(PlatformError):
        platform_token.verify(tok, keys=[SigningKey.generate()], issuer="https://api", audience="https://mcp")


def test_prod_settings_reject_dev_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from aip.libs.common.config import Settings

    monkeypatch.setenv("AIP_ENV", "prod")
    with pytest.raises(ValueError, match="secret_box_key"):
        Settings()
