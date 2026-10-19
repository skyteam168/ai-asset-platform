"""令牌端点与元数据（architecture.md 3.1 模式 A、3.3）。

- client_credentials：应用身份令牌。
- urn:ietf:params:oauth:grant-type:jwt-bearer（RFC 7523）：用户断言换用户令牌，act.sub = client_id。
客户端认证：private_key_jwt（client_assertion，RFC 7523 第 2.2 节）或静态 API Token（Authorization: Bearer）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Form, Header, Request

from aip.data_plane.principal import Principal, PrincipalResolver
from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens import platform_token
from aip.libs.tokens.assertion import verify_assertion
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.replay import ReplayStore
from aip.libs.tokens.secrets_util import token_hash

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


@dataclass
class AuthContext:
    settings: Settings
    resolver: PrincipalResolver
    key: SigningKey
    replay: ReplayStore

    @property
    def audiences(self) -> list[str]:
        return [self.settings.mcp_resource, self.settings.issuer]

    async def authenticate_client(
        self,
        authorization: str | None,
        client_id: str | None,
        client_assertion_type: str | None,
        client_assertion: str | None,
    ) -> str:
        if client_assertion:
            if client_assertion_type != CLIENT_ASSERTION_TYPE or not client_id:
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "client_assertion 参数不完整")
            _, c = self.resolver.consumer(client_id)
            v = await verify_assertion(
                client_assertion,
                client_id=client_id,
                jwks=c["jwks"],
                audiences=[self.settings.token_endpoint_aud],
                replay=self.replay,
                max_ttl_s=self.settings.assertion_max_ttl_s,
            )
            if v.subject != client_id:
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "client_assertion 的 sub 必须等于 client_id")
            return client_id
        if authorization and authorization.lower().startswith("bearer "):
            return self.resolver.static_token(token_hash(authorization[7:].strip()), dt.datetime.now(dt.UTC))
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "缺少客户端凭据")

    async def principal_from_request(self, authorization: str | None, user_assertion: str | None) -> Principal:
        """数据面每个受保护请求的入口：平台访问令牌，或静态令牌 +（可选）X-User-Assertion。"""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "缺少 Bearer 凭据")
        token = authorization[7:].strip()
        if token.startswith("aipk_"):
            client_id = self.resolver.static_token(token_hash(token), dt.datetime.now(dt.UTC))
            if not user_assertion:
                return self.resolver.for_app(client_id, auth_mode="app_token")
            _, c = self.resolver.consumer(client_id)
            v = await verify_assertion(
                user_assertion,
                client_id=client_id,
                jwks=c["jwks"],
                audiences=self.audiences,
                replay=self.replay,
                max_ttl_s=self.settings.assertion_max_ttl_s,
            )
            return self.resolver.for_assertion(v, "header_assertion")
        claims = None
        for aud in self.audiences:
            try:
                claims = platform_token.verify(token, keys=[self.key], issuer=self.settings.issuer, audience=aud)
                break
            except PlatformError as e:
                if e.code == ErrorCode.TOKEN_EXPIRED:
                    raise
        if claims is None:
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "访问令牌无效")
        if claims.kind == "app":
            return self.resolver.for_app(
                claims.client_id, auth_mode="token_exchange", agent_profile=claims.agent_profile
            )
        return self.resolver.for_user(claims.client_id, claims.subject, auth_mode="token_exchange")


def router(ctx: AuthContext) -> APIRouter:
    r = APIRouter(tags=["oauth"])
    s = ctx.settings

    @r.get("/.well-known/oauth-protected-resource", operation_id="protectedResourceMetadata")
    async def protected_resource() -> dict[str, Any]:
        return {"resource": s.mcp_resource, "authorization_servers": [s.issuer], "bearer_methods_supported": ["header"]}

    @r.get("/.well-known/oauth-authorization-server", operation_id="authorizationServerMetadata")
    async def authorization_server() -> dict[str, Any]:
        return {
            "issuer": s.issuer,
            "token_endpoint": f"{s.issuer}/oauth/token",
            "jwks_uri": f"{s.issuer}/.well-known/jwks.json",
            "grant_types_supported": ["client_credentials", JWT_BEARER],
            "token_endpoint_auth_methods_supported": ["private_key_jwt"],
            "token_endpoint_auth_signing_alg_values_supported": ["ES256", "RS256"],
        }

    @r.get("/.well-known/jwks.json", operation_id="jwks")
    async def jwks() -> dict[str, Any]:
        return {"keys": [ctx.key.public_jwk()]}

    @r.post("/oauth/token", operation_id="token")
    async def token(
        request: Request,
        grant_type: Annotated[str, Form()],
        assertion: Annotated[str | None, Form()] = None,
        client_id: Annotated[str | None, Form()] = None,
        client_assertion_type: Annotated[str | None, Form()] = None,
        client_assertion: Annotated[str | None, Form()] = None,
        resource: Annotated[str | None, Form()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        audience = resource or s.mcp_resource
        if audience not in ctx.audiences:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "resource 不是本平台的受保护资源")
        cid = await ctx.authenticate_client(authorization, client_id, client_assertion_type, client_assertion)
        if grant_type == "client_credentials":
            ctx.resolver.for_app(cid, auth_mode="token_exchange")
            tok, ttl = platform_token.issue(
                ctx.key,
                issuer=s.issuer,
                audience=audience,
                kind="app",
                subject=cid,
                client_id=cid,
                agent_profile=None,
                ttl_s=s.access_token_ttl_s,
            )
        elif grant_type == JWT_BEARER:
            if not assertion:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "缺少 assertion")
            _, c = ctx.resolver.consumer(cid)
            v = await verify_assertion(
                assertion,
                client_id=cid,
                jwks=c["jwks"],
                audiences=[s.token_endpoint_aud, *ctx.audiences],
                replay=ctx.replay,
                max_ttl_s=s.assertion_max_ttl_s,
            )
            p = ctx.resolver.for_assertion(v, "token_exchange")
            tok, ttl = platform_token.issue(
                ctx.key,
                issuer=s.issuer,
                audience=audience,
                kind="user",
                subject=str(p.user_id),
                client_id=cid,
                agent_profile=v.act_agent,
                ttl_s=s.access_token_ttl_s,
            )
        else:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "不支持的 grant_type")
        return {"access_token": tok, "token_type": "Bearer", "expires_in": ttl}

    return r
