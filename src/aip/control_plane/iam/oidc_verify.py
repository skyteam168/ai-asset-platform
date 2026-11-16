"""OIDC id_token 校验（G3.1）：仅提取 sub；禁止信任 org/role/clearance claim。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from urllib.parse import urlparse

import jwt
from jwt import PyJWK

from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient import DEFAULT_ALLOWLIST, ConnectionConfig, build_client, map_error


def _aud_ok(aud: object, expected: str) -> bool:
    if isinstance(aud, list):
        return expected in aud
    return aud == expected


async def fetch_oidc_jwks(jwks_url: str) -> dict[str, Any]:
    """经 build_client 拉取 JWKS（architecture.md 9.6）。"""
    parsed = urlparse(jwks_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "oidc_jwks_url 无效")
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    scheme = parsed.scheme  # type: ignore[assignment]
    exp = dt.datetime.now(dt.UTC) + dt.timedelta(days=1) if scheme == "http" else None
    cfg = ConnectionConfig(
        id="oidc-jwks",
        base_url=base,
        scheme=scheme,  # type: ignore[arg-type]
        tls_mode=None if scheme == "http" else "system_ca",
        transport_exception_reason="OIDC JWKS fetch" if scheme == "http" else None,
        transport_exception_expires_at=exp,
        status="active",
        connect_timeout_s=5.0,
        read_timeout_s=10.0,
    )
    try:
        async with build_client(cfg, allowlist=DEFAULT_ALLOWLIST, trace_id="oidc-jwks") as client:
            resp = await client.get(path)
    except Exception as e:
        raise map_error(e) from e
    if resp.status_code >= 400:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, f"JWKS HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as e:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "JWKS 非 JSON") from e
    if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "JWKS 格式无效")
    return body


def verify_id_token_claims(
    token: str,
    *,
    settings: Settings,
    jwks: dict[str, Any],
) -> str:
    """校验签名与 iss/aud，返回 sub。忽略一切授权相关 claim。"""
    if not settings.oidc_issuer:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 oidc_issuer")
    expected_aud = settings.oidc_audience or settings.oidc_client_id
    if not expected_aud:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 oidc_audience / oidc_client_id")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token 无效") from e
    kid = header.get("kid")
    keys = [k for k in jwks.get("keys") or [] if isinstance(k, dict)]
    jwk_obj: dict[str, Any] | None = None
    if kid:
        jwk_obj = next((k for k in keys if k.get("kid") == kid), None)
    elif len(keys) == 1:
        jwk_obj = keys[0]
    if jwk_obj is None:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token 密钥未知")
    alg = str(header.get("alg") or jwk_obj.get("alg") or "RS256")
    try:
        key = PyJWK.from_json(json.dumps(jwk_obj), algorithm=alg)
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key.key,
            algorithms=[alg],
            audience=expected_aud,
            issuer=settings.oidc_issuer,
            options={"require": ["sub", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise PlatformError(ErrorCode.TOKEN_EXPIRED, "id_token 已过期") from e
    except jwt.PyJWTError as e:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token 无效") from e
    # 双保险：不信任外部角色/组织（即使 decode 带进来也丢弃）
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token 缺少 sub")
    if not _aud_ok(claims.get("aud"), expected_aud):
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token audience 不匹配")
    return sub.strip()


async def verify_oidc_id_token(token: str, settings: Settings, *, jwks: dict[str, Any] | None = None) -> str:
    material = jwks if jwks is not None else await fetch_oidc_jwks(settings.oidc_jwks_url)
    return verify_id_token_claims(token, settings=settings, jwks=material)


def build_oidc_authorization_url(
    settings: Settings,
    *,
    state: str,
    code_challenge: str,
    redirect_uri: str | None = None,
) -> str:
    """拼装 authorization_code + PKCE S256 跳转 URL。"""
    from urllib.parse import urlencode

    auth_url = (settings.oidc_authorization_url or "").strip()
    if not auth_url:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 oidc_authorization_url")
    client_id = (settings.oidc_client_id or "").strip()
    if not client_id:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 oidc_client_id")
    redir = (redirect_uri or settings.oidc_redirect_uri or "").strip()
    if not redir:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 oidc_redirect_uri")
    q = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redir,
            "scope": "openid",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in auth_url else "?"
    return f"{auth_url}{sep}{q}"


async def exchange_oidc_code(
    *,
    code: str,
    code_verifier: str,
    settings: Settings,
    redirect_uri: str | None = None,
) -> str:
    """授权码换 id_token（经 build_client；architecture.md 9.6）。"""
    token_url = (settings.oidc_token_url or "").strip()
    if not token_url:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 oidc_token_url")
    redir = (redirect_uri or settings.oidc_redirect_uri or "").strip()
    if not redir:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 oidc_redirect_uri")
    client_id = (settings.oidc_client_id or "").strip()
    if not client_id:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "未配置 oidc_client_id")

    parsed = urlparse(token_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "oidc_token_url 无效")
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    scheme = parsed.scheme  # type: ignore[assignment]
    exp = dt.datetime.now(dt.UTC) + dt.timedelta(days=1) if scheme == "http" else None
    cfg = ConnectionConfig(
        id="oidc-token",
        base_url=base,
        scheme=scheme,  # type: ignore[arg-type]
        tls_mode=None if scheme == "http" else "system_ca",
        transport_exception_reason="OIDC token exchange" if scheme == "http" else None,
        transport_exception_expires_at=exp,
        status="active",
        connect_timeout_s=5.0,
        read_timeout_s=15.0,
    )
    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redir,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    secret = (settings.oidc_client_secret or "").strip()
    if secret:
        form["client_secret"] = secret
    try:
        async with build_client(cfg, allowlist=DEFAULT_ALLOWLIST, trace_id="oidc-token") as client:
            resp = await client.post(path, data=form)
    except Exception as e:
        raise map_error(e) from e
    if resp.status_code >= 400:
        raise PlatformError(ErrorCode.UNAUTHENTICATED, f"换票失败 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as e:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "token 响应非 JSON") from e
    if not isinstance(body, dict):
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "token 响应无效")
    id_token = body.get("id_token")
    if not isinstance(id_token, str) or not id_token.strip():
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "换票响应缺少 id_token")
    return id_token.strip()
