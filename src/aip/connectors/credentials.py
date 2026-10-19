"""Credential Manager（architecture.md 9.2）：不每次调用都登录；Token 缓存并续期；刷新 single-flight；
遇 401 刷新后最多重试一次；凭据永不进入模型上下文与日志。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from aip.connectors.jsonpath import first
from aip.libs.common.errors import ErrorCode, PlatformError


class SecretStore(Protocol):
    async def get(self, ref: str) -> dict[str, str]: ...


class MemorySecretStore:
    def __init__(self, secrets: dict[str, dict[str, str]] | None = None) -> None:
        self.secrets = secrets or {}

    async def get(self, ref: str) -> dict[str, str]:
        if ref not in self.secrets:
            raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游凭据不存在")
        return self.secrets[ref]


class OpenBaoSecretStore:
    """KV v2：credential_ref 形如 kv/data/mes/vn01。只在内存中短暂持有明文。"""

    def __init__(self, addr: str, token: str) -> None:
        self._http = httpx.AsyncClient(base_url=addr, headers={"X-Vault-Token": token}, timeout=3)

    async def get(self, ref: str) -> dict[str, str]:
        r = await self._http.get(f"/v1/{ref}")
        if r.status_code != 200:
            raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "读取下游凭据失败")
        data: dict[str, str] = r.json()["data"]["data"]
        return data


class CredentialManager:
    """auth 配置示例：
    {"mode": "api_key", "header": "X-API-Key"}
    {"mode": "bearer"}
    {"mode": "login", "path": "/api/login", "token_path": "$.data.token", "ttl_s": 1800}
    """

    def __init__(self, auth: dict[str, Any], secret_ref: str | None, store: SecretStore) -> None:
        self.auth, self.ref, self.store = auth, secret_ref, store
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()
        self.login_count = 0

    async def headers(self, client_factory: Callable[[], httpx.AsyncClient]) -> dict[str, str]:
        mode = self.auth.get("mode", "none")
        if mode == "none":
            return {}
        if not self.ref:
            raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "连接未配置凭据")
        if mode == "api_key":
            return {self.auth.get("header", "X-API-Key"): (await self.store.get(self.ref))["api_key"]}
        if mode == "bearer":
            return {"Authorization": f"Bearer {(await self.store.get(self.ref))['token']}"}
        if mode == "login":
            return {
                self.auth.get("header", "Authorization"): self.auth.get("prefix", "Bearer ")
                + await self._login_token(client_factory)
            }
        raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, f"不支持的认证方式 {mode}")

    async def _login_token(self, client_factory: Callable[[], httpx.AsyncClient]) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        async with self._lock:  # single-flight：并发调用只触发一次登录
            if self._token and time.monotonic() < self._expires:
                return self._token
            secret = await self.store.get(self.ref or "")
            self.login_count += 1
            async with client_factory() as c:
                r = await c.post(
                    self.auth.get("path", "/login"),
                    json={
                        self.auth.get("username_field", "username"): secret["username"],
                        self.auth.get("password_field", "password"): secret["password"],
                    },
                )
            if r.status_code != 200:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录失败，需人工处理")
            found = first(self.auth.get("token_path", "$.token"), r.json())
            if found is None:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录响应中没有令牌")
            self._token = str(found)
            self._expires = time.monotonic() + float(self.auth.get("ttl_s", 1800)) * 0.9
            return self._token

    def invalidate(self, token_header_value: str | None = None) -> None:
        """401 后调用；只作废引发 401 的那个令牌，避免并发请求反复作废新令牌。"""
        if token_header_value is None or (self._token and token_header_value.endswith(self._token)):
            self._token, self._expires = None, 0.0
