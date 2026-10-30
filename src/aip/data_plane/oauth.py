"""令牌端点与元数据（architecture.md 3.1 模式 A、3.3）。

- client_credentials：应用身份令牌。
- urn:ietf:params:oauth:grant-type:jwt-bearer（RFC 7523）：用户断言换用户令牌，act.sub = client_id。
- authorization_code + PKCE（H52 / G3.2）：个人 MCP 客户端；GET 同意页 + redirect。
客户端认证：private_key_jwt（client_assertion，RFC 7523 第 2.2 节）或静态 API Token（Authorization: Bearer）。
"""

from __future__ import annotations

import datetime as dt
import html
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from aip.data_plane.pkce import AuthCodeStore, MemoryAuthCodeStore, issue_auth_code, verify_pkce
from aip.data_plane.principal import Principal, PrincipalResolver
from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens import platform_token
from aip.libs.tokens.assertion import VerifiedAssertion, verify_assertion
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
    verify_keys: list[SigningKey] = field(default_factory=list)
    auth_codes: AuthCodeStore = field(default_factory=MemoryAuthCodeStore)

    # 用户断言验签失败时回调（client_id, 错误码），用于“断言验签失败激增”风险规则（12.4）
    on_assertion_failure: Callable[[str, str], None] | None = None

    def __post_init__(self) -> None:
        if not self.verify_keys:
            self.verify_keys = [self.key]

    async def verify_user_assertion(
        self, token: str, client_id: str, consumer: dict[str, Any], audiences: list[str]
    ) -> VerifiedAssertion:
        try:
            return await verify_assertion(
                token,
                client_id=client_id,
                jwks=consumer["jwks"],
                audiences=audiences,
                replay=self.replay,
                max_ttl_s=self.settings.assertion_max_ttl_s,
            )
        except PlatformError as e:
            if self.on_assertion_failure is not None:
                self.on_assertion_failure(client_id, str(e.code))
            raise

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
            v = await self.verify_user_assertion(user_assertion, client_id, c, self.audiences)
            return self.resolver.for_assertion(v, "header_assertion")
        claims = None
        for aud in self.audiences:
            try:
                claims = platform_token.verify(token, keys=self.verify_keys, issuer=self.settings.issuer, audience=aud)
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


class AuthorizeStubIn(BaseModel):
    """骨架：无浏览器同意页时用 stub 签发授权码（须 AIP_OAUTH_PKCE_STUB=true）。"""

    client_id: str
    redirect_uri: str = Field(min_length=1, max_length=2048)
    code_challenge: str = Field(min_length=43, max_length=128)
    code_challenge_method: str = "S256"
    subject: str = Field(min_length=1, max_length=256, description="消费方侧员工标识，经 user_binding 映射")
    resource: str | None = None


def router(ctx: AuthContext) -> APIRouter:
    r = APIRouter(tags=["oauth"])
    s = ctx.settings

    @r.get("/.well-known/oauth-protected-resource", operation_id="protectedResourceMetadata")
    async def protected_resource() -> dict[str, Any]:
        return {"resource": s.mcp_resource, "authorization_servers": [s.issuer], "bearer_methods_supported": ["header"]}

    @r.get("/.well-known/oauth-authorization-server", operation_id="authorizationServerMetadata")
    async def authorization_server() -> dict[str, Any]:
        grants = ["client_credentials", JWT_BEARER]
        meta: dict[str, Any] = {
            "issuer": s.issuer,
            "token_endpoint": f"{s.issuer}/oauth/token",
            "jwks_uri": f"{s.issuer}/.well-known/jwks.json",
            "grant_types_supported": grants,
            "token_endpoint_auth_methods_supported": ["private_key_jwt"],
            "token_endpoint_auth_signing_alg_values_supported": ["ES256", "RS256"],
        }
        if s.oauth_pkce_enabled:
            grants.append("authorization_code")
            meta["authorization_endpoint"] = f"{s.issuer}/oauth/authorize"
            meta["code_challenge_methods_supported"] = ["S256"]
            meta["response_types_supported"] = ["code"]
        return meta

    @r.get("/.well-known/jwks.json", operation_id="jwks")
    async def jwks() -> dict[str, Any]:
        return {"keys": [k.public_jwk() for k in ctx.verify_keys]}

    def _issue_code(
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        subject: str,
        resource: str | None,
    ) -> tuple[str, str]:
        if not redirect_uri.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "redirect_uri 不在允许列表")
        consumer_id, c = ctx.resolver.consumer(client_id)
        user_id = ctx.resolver.map_subject(consumer_id, c, subject)
        audience = resource or s.mcp_resource
        if audience not in ctx.audiences:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "resource 不是本平台的受保护资源")
        code = issue_auth_code(
            ctx.auth_codes,
            client_id=client_id,
            user_id=user_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method or "S256",
            resource=audience,
            ttl_s=s.oauth_pkce_code_ttl_s,
        )
        return code, audience

    @r.get("/oauth/authorize", operation_id="authorizeConsentPage", response_class=HTMLResponse)
    async def authorize_get(
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        response_type: str = "code",
        code_challenge_method: str = "S256",
        subject: str = "",
        resource: str | None = None,
        state: str | None = None,
    ) -> HTMLResponse:
        """G3.2：最小浏览器同意页（非 stub 主路径）。"""
        if not s.oauth_pkce_enabled:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "authorization_code + PKCE 未启用")
        if response_type != "code":
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅支持 response_type=code")
        if not subject.strip():
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "缺少 subject（员工标识，经绑定映射）")
        esc = html.escape
        state_field = (
            f'<input type="hidden" name="state" value="{esc(state)}" />' if state else ""
        )
        body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>授权同意</title>
<style>body{{font-family:system-ui;max-width:32rem;margin:2rem auto;padding:0 1rem}}
button{{padding:.5rem 1rem;margin-right:.5rem}}</style></head>
<body>
<h1>授权第三方访问</h1>
<p>应用 <strong>{esc(client_id)}</strong> 请求以员工标识 <code>{esc(subject)}</code> 访问平台资源。</p>
<p>回调：<code>{esc(redirect_uri)}</code></p>
<form method="post" action="/oauth/authorize/consent">
<input type="hidden" name="client_id" value="{esc(client_id)}" />
<input type="hidden" name="redirect_uri" value="{esc(redirect_uri)}" />
<input type="hidden" name="code_challenge" value="{esc(code_challenge)}" />
<input type="hidden" name="code_challenge_method" value="{esc(code_challenge_method)}" />
<input type="hidden" name="subject" value="{esc(subject)}" />
<input type="hidden" name="resource" value="{esc(resource or '')}" />
{state_field}
<button type="submit" name="decision" value="allow">同意</button>
<button type="submit" name="decision" value="deny">拒绝</button>
</form>
</body></html>"""
        return HTMLResponse(body)

    @r.post("/oauth/authorize/consent", operation_id="authorizeConsent")
    async def authorize_consent(
        client_id: Annotated[str, Form()],
        redirect_uri: Annotated[str, Form()],
        code_challenge: Annotated[str, Form()],
        subject: Annotated[str, Form()],
        decision: Annotated[str, Form()] = "allow",
        code_challenge_method: Annotated[str, Form()] = "S256",
        resource: Annotated[str | None, Form()] = None,
        state: Annotated[str | None, Form()] = None,
    ) -> RedirectResponse:
        if not s.oauth_pkce_enabled:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "authorization_code + PKCE 未启用")
        q: dict[str, str] = {}
        if state:
            q["state"] = state
        if decision != "allow":
            q["error"] = "access_denied"
            return RedirectResponse(f"{redirect_uri}?{urlencode(q)}", status_code=302)
        code, _ = _issue_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            subject=subject,
            resource=resource or None,
        )
        q["code"] = code
        return RedirectResponse(f"{redirect_uri}?{urlencode(q)}", status_code=302)

    @r.post("/oauth/authorize", operation_id="authorizeStub", response_model=None)
    async def authorize_stub(body: AuthorizeStubIn) -> dict[str, Any]:
        """骨架：无浏览器时 stub 签发 code（须 AIP_OAUTH_PKCE_STUB=true）。"""
        if not s.oauth_pkce_enabled:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "authorization_code + PKCE 未启用")
        if not s.oauth_pkce_stub:
            raise PlatformError(
                ErrorCode.PRECONDITION_FAILED,
                "非 stub 模式请用 GET /oauth/authorize 浏览器同意页",
            )
        code, _ = _issue_code(
            client_id=body.client_id,
            redirect_uri=body.redirect_uri,
            code_challenge=body.code_challenge,
            code_challenge_method=body.code_challenge_method,
            subject=body.subject,
            resource=body.resource,
        )
        return {
            "code": code,
            "expires_in": s.oauth_pkce_code_ttl_s,
            "redirect_uri": body.redirect_uri,
            "state": None,
        }

    @r.post("/oauth/token", operation_id="token")
    async def token(
        request: Request,
        grant_type: Annotated[str, Form()],
        assertion: Annotated[str | None, Form()] = None,
        client_id: Annotated[str | None, Form()] = None,
        client_assertion_type: Annotated[str | None, Form()] = None,
        client_assertion: Annotated[str | None, Form()] = None,
        resource: Annotated[str | None, Form()] = None,
        code: Annotated[str | None, Form()] = None,
        redirect_uri: Annotated[str | None, Form()] = None,
        code_verifier: Annotated[str | None, Form()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if grant_type == "authorization_code":
            if not s.oauth_pkce_enabled:
                raise PlatformError(ErrorCode.PRECONDITION_FAILED, "authorization_code + PKCE 未启用")
            if not code or not redirect_uri or not code_verifier or not client_id:
                raise PlatformError(
                    ErrorCode.INVALID_ARGUMENT, "authorization_code 缺少 code/redirect_uri/code_verifier/client_id"
                )
            cid = await ctx.authenticate_client(authorization, client_id, client_assertion_type, client_assertion)
            if cid != client_id:
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "client_id 与凭据不一致")
            rec = ctx.auth_codes.take(code)
            if rec is None:
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "授权码无效或已使用")
            if rec.client_id != cid or rec.redirect_uri != redirect_uri:
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "授权码与 client/redirect 不匹配")
            verify_pkce(code_verifier, rec.code_challenge, rec.code_challenge_method)
            if rec.resource not in ctx.audiences:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "resource 不是本平台的受保护资源")
            p = ctx.resolver.for_user(cid, rec.user_id, auth_mode="token_exchange")
            tok, ttl = platform_token.issue(
                ctx.key,
                issuer=s.issuer,
                audience=rec.resource,
                kind="user",
                subject=str(p.user_id),
                client_id=cid,
                agent_profile=None,
                ttl_s=s.access_token_ttl_s,
            )
            return {"access_token": tok, "token_type": "Bearer", "expires_in": ttl}

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
            v = await ctx.verify_user_assertion(assertion, cid, c, [s.token_endpoint_aud, *ctx.audiences])
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
