"""登录插件 AuthProvider（architecture.md 3.3）：只验证身份并返回外部唯一标识。

OIDC / SAML 插件不得读取或信任外部返回的组织、角色、密级；映射只经 external_login。
本片提供 local 登记与 OIDC stub（不接真 IdP）；真 Keycloak 联调属 Wave B 外部验收。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError


@dataclass(frozen=True)
class ExternalIdentity:
    """插件验证成功后的外部主体；不含任何授权声明。"""

    provider: str
    external_id: str


class AuthProvider(Protocol):
    name: str

    def enabled(self, settings: Settings) -> bool: ...

    async def authenticate(self, payload: dict[str, Any], settings: Settings) -> ExternalIdentity: ...


class LocalAuthProvider:
    """内置本地账号：实际口令/TOTP 校验仍走 auth.login；此处仅作扩展点登记。"""

    name = "local"

    def enabled(self, settings: Settings) -> bool:
        return True

    async def authenticate(self, payload: dict[str, Any], settings: Settings) -> ExternalIdentity:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "本地登录请使用 POST /auth/login")


class OidcStubAuthProvider:
    """OIDC 骨架：stub 模式下接受 id_token=stub:<sub> 或 JSON {\"sub\":...}。

    开启条件：AIP_OIDC_ENABLED=true 且 AIP_OIDC_STUB=true（默认 true）。
    生产必须关掉 stub，改接真实 JWKS 校验（后续切片）。
    """

    name = "oidc"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.oidc_enabled)

    async def authenticate(self, payload: dict[str, Any], settings: Settings) -> ExternalIdentity:
        if not settings.oidc_enabled:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "OIDC 登录未启用")
        if not settings.oidc_stub:
            raise PlatformError(
                ErrorCode.PRECONDITION_FAILED,
                "OIDC 非 stub 模式尚未接线；请设置 AIP_OIDC_STUB=true 做本地验收，或接企业 IdP",
            )
        token = payload.get("id_token")
        if not isinstance(token, str) or not token.strip():
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "缺少 id_token")
        token = token.strip()
        sub: str | None = None
        if token.startswith("stub:"):
            sub = token[5:].strip()
        else:
            try:
                data = json.loads(token)
            except json.JSONDecodeError as e:
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token 无效") from e
            if not isinstance(data, dict):
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token 无效")
            raw_sub = data.get("sub")
            if isinstance(raw_sub, str):
                sub = raw_sub.strip()
            iss = data.get("iss")
            if settings.oidc_issuer and isinstance(iss, str) and iss != settings.oidc_issuer:
                raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token issuer 不匹配")
            aud = data.get("aud")
            expected_aud = settings.oidc_audience or settings.oidc_client_id
            if expected_aud:
                if isinstance(aud, list):
                    ok = expected_aud in aud
                else:
                    ok = aud == expected_aud
                if not ok:
                    raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token audience 不匹配")
        if not sub:
            raise PlatformError(ErrorCode.UNAUTHENTICATED, "id_token 缺少 sub")
        return ExternalIdentity(provider=self.name, external_id=sub)


_REGISTRY: dict[str, AuthProvider] = {
    LocalAuthProvider.name: LocalAuthProvider(),
    OidcStubAuthProvider.name: OidcStubAuthProvider(),
}


def get_provider(name: str) -> AuthProvider:
    p = _REGISTRY.get(name)
    if p is None:
        raise PlatformError(ErrorCode.NOT_FOUND, f"未知登录插件：{name}")
    return p


def list_providers(settings: Settings | None = None) -> list[dict[str, Any]]:
    s = settings or get_settings()
    return [{"name": p.name, "enabled": p.enabled(s)} for p in _REGISTRY.values()]
