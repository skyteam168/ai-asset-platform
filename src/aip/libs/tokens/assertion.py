"""用户断言验签（architecture.md 3.1）。

断言 = 消费方用登记私钥签名的 JWT：
iss=client_id、sub=员工在消费方侧标识、aud、iat、exp（≤ 5 分钟）、jti、可选 act_agent。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWK

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.replay import ReplayStore

ALLOWED_ALGS = ("ES256", "RS256")
LEEWAY_S = 30
DEGRADED_MAX_REMAINING_S = 60


@dataclass(frozen=True)
class VerifiedAssertion:
    client_id: str
    subject: str
    jti: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    act_agent: str | None


def _fail(code: ErrorCode, detail: str) -> PlatformError:
    return PlatformError(code, detail)


async def verify_assertion(
    token: str,
    *,
    client_id: str,
    jwks: dict[str, dict[str, Any]],
    audiences: list[str],
    replay: ReplayStore,
    max_ttl_s: int = 300,
    now: dt.datetime | None = None,
) -> VerifiedAssertion:
    """jwks: kid -> JWK（仅 active 的公钥）。任何失败都抛 PlatformError，且 detail 不回显断言内容。"""
    now = now or dt.datetime.now(dt.UTC)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise _fail(ErrorCode.ASSERTION_INVALID, "断言格式错误") from e
    alg, kid = header.get("alg"), header.get("kid")
    if alg not in ALLOWED_ALGS:
        raise _fail(ErrorCode.ASSERTION_INVALID, "不支持的签名算法")
    if not kid or kid not in jwks:
        raise _fail(ErrorCode.ASSERTION_INVALID, "未知或已吊销的签名公钥")
    try:
        key = PyJWK.from_json(json.dumps(jwks[kid]), algorithm=alg)
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=[alg],
            audience=audiences,
            issuer=client_id,
            leeway=LEEWAY_S,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise _fail(ErrorCode.ASSERTION_EXPIRED, "断言已过期") from e
    except jwt.InvalidAudienceError as e:
        raise _fail(ErrorCode.ASSERTION_INVALID, "断言 aud 不正确") from e
    except jwt.InvalidIssuerError as e:
        raise _fail(ErrorCode.ASSERTION_INVALID, "断言 iss 与消费方不符") from e
    except jwt.PyJWTError as e:
        raise _fail(ErrorCode.ASSERTION_INVALID, "断言签名或声明无效") from e

    iat = dt.datetime.fromtimestamp(int(claims["iat"]), dt.UTC)
    exp = dt.datetime.fromtimestamp(int(claims["exp"]), dt.UTC)
    if iat > now + dt.timedelta(seconds=LEEWAY_S):
        raise _fail(ErrorCode.ASSERTION_INVALID, "断言 iat 在未来")
    if (exp - iat).total_seconds() > max_ttl_s:
        raise _fail(ErrorCode.ASSERTION_EXPIRED, f"断言有效期超过 {max_ttl_s} 秒")
    remaining = (exp - now).total_seconds() + LEEWAY_S
    if replay.degraded and remaining > DEGRADED_MAX_REMAINING_S + LEEWAY_S:
        raise _fail(ErrorCode.ASSERTION_EXPIRED, "防重放存储降级中，只接受剩余有效期 ≤ 60 秒的断言")
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise _fail(ErrorCode.ASSERTION_INVALID, "断言 sub 无效")
    if not await replay.first_use(f"{client_id}:{claims['jti']}", int(remaining) + 1):
        raise _fail(ErrorCode.ASSERTION_REPLAYED, "断言已被使用")
    act = claims.get("act_agent")
    return VerifiedAssertion(
        client_id, claims["sub"], str(claims["jti"]), iat, exp, act if isinstance(act, str) else None
    )
