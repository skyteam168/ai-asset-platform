"""Credential Manager（architecture.md 9.2）：不每次调用都登录；Token 缓存并续期；刷新 single-flight；
遇 401 刷新后最多重试一次；凭据永不进入模型上下文与日志。

login 模式支持声明式握手（prepare → transform → body），覆盖企业常见的
「取公钥 → RSA/SM2 加密口令 → 换 JWT」而不引入任意脚本。

cookie 模式：form/json 登录 → 缓存 Set-Cookie 中的会话名（如 PHPSESSID）→ 注入 Cookie 头。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import time
import uuid
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import Any, Protocol

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from aip.connectors.jsonpath import first
from aip.libs.common.errors import ErrorCode, PlatformError

_SUCCESS_EXPR = re.compile(r"^\s*(\$[^\s=!]*)\s*(==|!=)\s*(.+?)\s*$")


class SecretStore(Protocol):
    async def get(self, ref: str) -> dict[str, str]: ...

    async def put(self, ref: str, data: dict[str, str]) -> None: ...


class MemorySecretStore:
    def __init__(self, secrets: dict[str, dict[str, str]] | None = None) -> None:
        self.secrets = secrets or {}

    async def get(self, ref: str) -> dict[str, str]:
        if ref not in self.secrets:
            raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游凭据不存在")
        return self.secrets[ref]

    async def put(self, ref: str, data: dict[str, str]) -> None:
        self.secrets[ref] = dict(data)


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

    async def put(self, ref: str, data: dict[str, str]) -> None:
        """写入 KV v2；路径与 get 相同（含 data/ 段）。"""
        r = await self._http.post(f"/v1/{ref}", json={"data": data})
        if r.status_code not in (200, 204):
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, f"写入凭据失败：HTTP {r.status_code}")


_ALLOWED_TRANSFORM_OPS = frozenset({"rsa_encrypt_json", "rsa_encrypt", "base64", "json_stringify"})


class CredentialManager:
    """auth 配置示例：
    {"mode": "api_key", "header": "X-API-Key"}
    {"mode": "bearer"}
    {"mode": "login", "path": "/api/login", "token_path": "$.data.token", "ttl_s": 1800}
    {"mode": "login", "path": "/api/v1/backend_login", "token_path": "$.data.list.token",
     "ttl_from": "jwt_exp",
     "prepare": [{"method": "GET", "path": "/api/v1/auth/public-key",
                  "capture": {"public_key": "$.data.publicKey", "key_id": "$.data.keyId"}}],
     "transform": [{"op": "rsa_encrypt_json", "public_key": "{{capture.public_key}}",
                    "padding": "PKCS1v15", "as": "encrypted_password",
                    "plaintext": {"password": "{{secret.password}}", "ts": "{{now_ms}}",
                                  "nonce": "{{random_hex:8}}"}}],
     "body": {"username": "{{secret.username}}", "encryptedPassword": "{{vars.encrypted_password}}",
              "keyId": "{{capture.key_id}}", "deviceId": "{{uuid}}", "deviceType": "aip-connector"}}
    {"mode": "cookie", "path": "/Index/login/login_do.html", "body_encoding": "form",
     "username_field": "user_name", "password_field": "password",
     "cookie_name": "PHPSESSID", "success_when": "$.code == 1", "ttl_s": 7200}
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
        if mode == "cookie":
            name = str(self.auth.get("cookie_name") or "PHPSESSID")
            return {"Cookie": f"{name}={await self._login_cookie(client_factory)}"}
        raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, f"不支持的认证方式 {mode}")

    async def probe(self, client_factory: Callable[[], httpx.AsyncClient]) -> dict[str, Any]:
        """测登录：走 headers 一次，不回传令牌/Cookie 明文。"""
        mode = str(self.auth.get("mode") or "none")
        if mode == "none":
            return {"ok": True, "mode": mode, "detail": "无需登录"}
        await self.headers(client_factory)
        out: dict[str, Any] = {"ok": True, "mode": mode, "detail": "认证成功"}
        if mode == "cookie":
            out["cookie_name"] = str(self.auth.get("cookie_name") or "PHPSESSID")
            left = max(0, int(self._expires - time.monotonic()))
            out["expires_in_s"] = left
        return out

    async def _login_token(self, client_factory: Callable[[], httpx.AsyncClient]) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        async with self._lock:  # single-flight：并发调用只触发一次登录
            if self._token and time.monotonic() < self._expires:
                return self._token
            secret = await self.store.get(self.ref or "")
            self.login_count += 1
            capture: dict[str, Any] = {}
            variables: dict[str, Any] = {}
            async with client_factory() as c:
                await self._run_prepare(c, capture)
                self._run_transforms(secret, capture, variables)
                body = self._build_login_body(secret, capture, variables)
                r = await c.post(self.auth.get("path", "/login"), json=body)
            if r.status_code != 200:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录失败，需人工处理")
            try:
                doc = r.json()
            except Exception as e:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录响应不是 JSON") from e
            found = first(self.auth.get("token_path", "$.token"), doc)
            if found is None:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录响应中没有令牌")
            token = str(found)
            self._token = token
            self._expires = time.monotonic() + self._token_ttl_s(token) * 0.9
            return token

    async def _login_cookie(self, client_factory: Callable[[], httpx.AsyncClient]) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            secret = await self.store.get(self.ref or "")
            self.login_count += 1
            path = str(self.auth.get("path") or "/login")
            if not path.startswith("/"):
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.path 必须以 / 开头")
            cookie_name = str(self.auth.get("cookie_name") or "PHPSESSID")
            body = self._build_login_body(secret, {}, {})
            encoding = str(self.auth.get("body_encoding") or "form").lower()
            async with client_factory() as c:
                if encoding == "json":
                    r = await c.post(path, json=body)
                elif encoding == "form":
                    r = await c.post(path, data={k: "" if v is None else str(v) for k, v in body.items()})
                else:
                    raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"不支持的 body_encoding: {encoding}")
            if r.status_code != 200:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录失败，需人工处理")
            success_when = self.auth.get("success_when")
            if success_when:
                try:
                    doc = r.json()
                except Exception as e:
                    raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录响应不是 JSON") from e
                if not _eval_success(str(success_when), doc):
                    raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录业务失败")
            value, max_age = _cookie_from_response(r, cookie_name)
            if not value:
                # 部分系统在客户端已有 Cookie 时只刷新；尝试 jar
                value = r.cookies.get(cookie_name) or ""
            if not value:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, f"下游登录未返回 Cookie {cookie_name}")
            ttl = float(max_age) if max_age is not None and max_age > 30 else float(self.auth.get("ttl_s", 1800))
            self._token = value
            self._expires = time.monotonic() + ttl * 0.9
            return value

    async def _run_prepare(self, client: httpx.AsyncClient, capture: dict[str, Any]) -> None:
        steps = self.auth.get("prepare") or []
        if not isinstance(steps, list):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.prepare 必须是数组")
        if len(steps) > 5:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.prepare 最多 5 步")
        for step in steps:
            if not isinstance(step, dict):
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.prepare 步骤必须是对象")
            method = str(step.get("method", "GET")).upper()
            if method not in ("GET", "POST"):
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"prepare 不支持方法 {method}")
            path = str(step.get("path") or "")
            if not path.startswith("/"):
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "prepare.path 必须以 / 开头")
            if method == "GET":
                r = await client.get(path)
            else:
                r = await client.post(path, json=step.get("body") or {})
            if r.status_code != 200:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游登录准备步骤失败")
            try:
                doc = r.json()
            except Exception as e:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "prepare 响应不是 JSON") from e
            for key, jpath in (step.get("capture") or {}).items():
                if not isinstance(key, str) or not key.isidentifier():
                    raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"非法 capture 名 {key}")
                val = first(str(jpath), doc)
                if val is None:
                    raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, f"prepare 未捕获 {key}")
                capture[key] = val

    def _run_transforms(
        self, secret: dict[str, str], capture: dict[str, Any], variables: dict[str, Any]
    ) -> None:
        steps = self.auth.get("transform") or []
        if not isinstance(steps, list):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.transform 必须是数组")
        if len(steps) > 8:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.transform 最多 8 步")
        for step in steps:
            if not isinstance(step, dict):
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.transform 步骤必须是对象")
            op = str(step.get("op") or "")
            if op not in _ALLOWED_TRANSFORM_OPS:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"不支持的 transform.op: {op}")
            as_name = str(step.get("as") or "")
            if not as_name.isidentifier():
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "transform.as 必须是标识符")
            if op == "rsa_encrypt_json":
                plaintext = step.get("plaintext")
                if not isinstance(plaintext, dict):
                    raise PlatformError(ErrorCode.INVALID_ARGUMENT, "rsa_encrypt_json 需要 plaintext 对象")
                rendered = {k: self._render(v, secret, capture, variables) for k, v in plaintext.items()}
                # ts / 数值字段：若渲染结果是纯数字字符串则转 int（对齐设备系统 envelope）
                for k, v in list(rendered.items()):
                    if isinstance(v, str) and v.isdigit():
                        rendered[k] = int(v)
                pem = self._render(step.get("public_key", "{{capture.public_key}}"), secret, capture, variables)
                pad = str(step.get("padding") or "PKCS1v15")
                variables[as_name] = _rsa_encrypt(
                    json.dumps(rendered, ensure_ascii=False, separators=(",", ":")),
                    str(pem),
                    pad,
                )
            elif op == "rsa_encrypt":
                raw = self._render(step.get("input", ""), secret, capture, variables)
                pem = self._render(step.get("public_key", "{{capture.public_key}}"), secret, capture, variables)
                variables[as_name] = _rsa_encrypt(str(raw), str(pem), str(step.get("padding") or "PKCS1v15"))
            elif op == "json_stringify":
                obj = step.get("value")
                if isinstance(obj, dict):
                    obj = {k: self._render(v, secret, capture, variables) for k, v in obj.items()}
                else:
                    obj = self._render(obj, secret, capture, variables)
                variables[as_name] = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            elif op == "base64":
                raw = self._render(step.get("input", ""), secret, capture, variables)
                variables[as_name] = base64.b64encode(str(raw).encode("utf-8")).decode("ascii")

    def _build_login_body(
        self, secret: dict[str, str], capture: dict[str, Any], variables: dict[str, Any]
    ) -> dict[str, Any]:
        template = self.auth.get("body")
        if template is None:
            # 兼容旧版：明文 username/password 字段名可配
            return {
                self.auth.get("username_field", "username"): secret["username"],
                self.auth.get("password_field", "password"): secret["password"],
            }
        if not isinstance(template, dict):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.body 必须是对象")
        out: dict[str, Any] = {}
        for k, v in template.items():
            rendered = self._render(v, secret, capture, variables)
            out[k] = rendered
        return out

    def _render(
        self, value: Any, secret: dict[str, str], capture: dict[str, Any], variables: dict[str, Any]
    ) -> Any:
        if not isinstance(value, str):
            return value
        # 整串替换占位符；仅支持白名单变量，禁止任意表达式
        s = value
        if s == "{{uuid}}":
            return str(uuid.uuid4())
        if s == "{{now_ms}}":
            return str(int(time.time() * 1000))
        if s == "{{now}}":
            return str(int(time.time()))
        if s.startswith("{{random_hex:") and s.endswith("}}"):
            try:
                n = int(s[len("{{random_hex:") : -2])
            except ValueError as e:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "random_hex 长度非法") from e
            if n < 1 or n > 64:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "random_hex 长度须在 1..64")
            return secrets.token_hex(n)
        for prefix, src in (("{{secret.", secret), ("{{capture.", capture), ("{{vars.", variables)):
            if s.startswith(prefix) and s.endswith("}}"):
                key = s[len(prefix) : -2]
                if key not in src:
                    raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, f"登录模板缺少变量 {key}")
                return src[key]
        # 允许不含占位符的字面量
        if "{{" in s:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"不支持的登录模板片段: {s[:40]}")
        return s

    def _token_ttl_s(self, token: str) -> float:
        mode = self.auth.get("ttl_from")
        if mode == "jwt_exp":
            exp = _jwt_exp(token)
            if exp is not None:
                skew = float(self.auth.get("ttl_skew_s", 120))
                remaining = exp - time.time() - skew
                if remaining > 30:
                    return remaining
            # JWT 无 exp 或已过期：退回 ttl_s
        return float(self.auth.get("ttl_s", 1800))

    def invalidate(self, token_header_value: str | None = None) -> None:
        """401 后调用；只作废引发 401 的那个令牌，避免并发请求反复作废新令牌。"""
        if token_header_value is None or (self._token and token_header_value.endswith(self._token)):
            self._token, self._expires = None, 0.0


def _eval_success(expr: str, doc: Any) -> bool:
    """与 declarative.evaluate 同形的迷你表达式，避免循环导入。"""
    m = _SUCCESS_EXPR.match(expr)
    if not m:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "auth.success_when 表达式不合法")
    path, op, literal = m.groups()
    value = first(path, doc)
    expected = json.loads(literal)
    return bool(value == expected) if op == "==" else bool(value != expected)


def _cookie_from_response(r: httpx.Response, cookie_name: str) -> tuple[str | None, int | None]:
    """从 Set-Cookie 解析指定 cookie 与 Max-Age。"""
    # httpx 可能合并多条；逐条解析
    raw_headers = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else []
    if not raw_headers:
        sc = r.headers.get("set-cookie")
        raw_headers = [sc] if sc else []
    for raw in raw_headers:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: S112 — 单条 Set-Cookie 畸形则跳过
            continue
        if cookie_name not in jar:
            continue
        morsel = jar[cookie_name]
        max_age_raw = morsel.get("max-age")
        max_age: int | None = None
        if max_age_raw:
            try:
                max_age = int(max_age_raw)
            except ValueError:
                max_age = None
        return morsel.value, max_age
    return None, None


def _jwt_exp(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _rsa_encrypt(plaintext: str, public_key_pem: str, pad_name: str) -> str:
    pem = public_key_pem.strip()
    if "BEGIN PUBLIC KEY" not in pem and "BEGIN RSA PUBLIC KEY" not in pem:
        # 允许裸 base64 DER → 包成 PEM
        pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(
            pem[i : i + 64] for i in range(0, len(pem), 64)
        ) + "\n-----END PUBLIC KEY-----"
    try:
        pub = serialization.load_pem_public_key(pem.encode("utf-8"))
    except Exception as e:
        raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游公钥无法解析") from e
    if not isinstance(pub, RSAPublicKey):
        raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "下游公钥不是 RSA")
    if pad_name.upper() in ("PKCS1V15", "PKCS1", "PKCS1_V1_5"):
        pad: padding.AsymmetricPadding = padding.PKCS1v15()
    else:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"不支持的 RSA padding: {pad_name}")
    try:
        cipher = pub.encrypt(plaintext.encode("utf-8"), pad)
    except Exception as e:
        raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "RSA 加密失败") from e
    return base64.b64encode(cipher).decode("ascii")
