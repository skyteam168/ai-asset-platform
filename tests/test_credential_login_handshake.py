"""Credential Manager 声明式登录握手：prepare + RSA 加密口令 + JWT ttl。"""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aip.connectors.credentials import CredentialManager, MemorySecretStore
from aip.libs.common.errors import ErrorCode, PlatformError


def _keypair() -> tuple[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return pub_pem, key


class _RsaLoginServer:
    """仿设备管家：GET public-key → POST backend_login（RSA 密文）→ JWT。"""

    def __init__(self) -> None:
        self.pub_pem, self.priv = _keypair()
        self.logins = 0
        self.last_body: dict[str, Any] | None = None
        self.valid_tokens: set[str] = set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if urlparse(self.path).path == "/api/v1/auth/public-key":
                    self._json(200, {"status": 200, "data": {"publicKey": outer.pub_pem, "keyId": "k1"}})
                    return
                if urlparse(self.path).path == "/api/v1/ping":
                    auth = self.headers.get("Authorization", "")
                    token = auth.removeprefix("Bearer ").strip()
                    if token in outer.valid_tokens:
                        self._json(200, {"ok": True})
                    else:
                        self._json(401, {"ok": False})
                    return
                self._json(404, {})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode() or "{}")
                if urlparse(self.path).path != "/api/v1/backend_login":
                    self._json(404, {})
                    return
                outer.logins += 1
                outer.last_body = body
                try:
                    cipher = base64.b64decode(body["encryptedPassword"])
                    plain = outer.priv.decrypt(cipher, padding.PKCS1v15()).decode()
                    env = json.loads(plain)
                except Exception:
                    self._json(200, {"status": 401, "message": "bad cipher"})
                    return
                if body.get("username") != "agent" or env.get("password") != "secret":
                    self._json(200, {"status": 401, "message": "bad creds"})
                    return
                token = jwt.encode(
                    {"sub": "agent", "exp": int(time.time()) + 600},
                    "test-secret",
                    algorithm="HS256",
                )
                outer.valid_tokens.add(token)
                self._json(200, {"status": 200, "data": {"list": {"token": token}}})

            def _json(self, status: int, doc: dict[str, Any]) -> None:
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()


AUTH = {
    "mode": "login",
    "path": "/api/v1/backend_login",
    "token_path": "$.data.list.token",
    "ttl_from": "jwt_exp",
    "prepare": [
        {
            "method": "GET",
            "path": "/api/v1/auth/public-key",
            "capture": {"public_key": "$.data.publicKey", "key_id": "$.data.keyId"},
        }
    ],
    "transform": [
        {
            "op": "rsa_encrypt_json",
            "public_key": "{{capture.public_key}}",
            "padding": "PKCS1v15",
            "as": "encrypted_password",
            "plaintext": {
                "password": "{{secret.password}}",
                "ts": "{{now_ms}}",
                "nonce": "{{random_hex:8}}",
            },
        }
    ],
    "body": {
        "username": "{{secret.username}}",
        "encryptedPassword": "{{vars.encrypted_password}}",
        "keyId": "{{capture.key_id}}",
        "deviceId": "{{uuid}}",
        "deviceType": "aip-connector",
    },
}


@pytest.mark.asyncio
async def test_rsa_login_handshake_caches_and_uses_jwt_exp() -> None:
    srv = _RsaLoginServer()
    try:
        mgr = CredentialManager(AUTH, "kv/data/eq", MemorySecretStore({"kv/data/eq": {"username": "agent", "password": "secret"}}))

        def factory() -> Any:
            import httpx

            return httpx.AsyncClient(base_url=srv.url, timeout=5)

        h1 = await mgr.headers(factory)
        h2 = await mgr.headers(factory)
        assert h1 == h2 and h1["Authorization"].startswith("Bearer ")
        assert srv.logins == 1
        assert srv.last_body is not None
        assert srv.last_body["username"] == "agent"
        assert srv.last_body["keyId"] == "k1"
        assert "encryptedPassword" in srv.last_body
        # JWT exp ~600s → 内部 expires 应远大于默认短 TTL 探测
        assert mgr._expires > time.monotonic() + 100
    finally:
        srv.close()


@pytest.mark.asyncio
async def test_rsa_login_rejects_bad_password() -> None:
    srv = _RsaLoginServer()
    try:
        mgr = CredentialManager(
            AUTH, "kv/data/eq", MemorySecretStore({"kv/data/eq": {"username": "agent", "password": "wrong"}})
        )

        def factory() -> Any:
            import httpx

            return httpx.AsyncClient(base_url=srv.url, timeout=5)

        with pytest.raises(PlatformError) as ei:
            await mgr.headers(factory)
        # 服务端仍 200 但无 token → DOWNSTREAM_AUTH_EXPIRED
        assert ei.value.code == ErrorCode.DOWNSTREAM_AUTH_EXPIRED
    finally:
        srv.close()


@pytest.mark.asyncio
async def test_plain_login_still_works() -> None:
    """旧版明文 login body 不受 prepare 影响。"""

    class Plain:
        def __init__(self) -> None:
            outer = self
            self.logins = 0

            class H(BaseHTTPRequestHandler):
                def do_POST(self) -> None:  # noqa: N802
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length).decode())
                    outer.logins += 1
                    ok = body.get("username") == "u" and body.get("password") == "p"
                    data = json.dumps({"data": {"token": "t-ok"}} if ok else {}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def log_message(self, *a: object) -> None:
                    pass

            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

        def close(self) -> None:
            self.httpd.shutdown()

    srv = Plain()
    try:
        mgr = CredentialManager(
            {"mode": "login", "path": "/api/login", "token_path": "$.data.token", "ttl_s": 60},
            "kv/x",
            MemorySecretStore({"kv/x": {"username": "u", "password": "p"}}),
        )

        def factory() -> Any:
            import httpx

            return httpx.AsyncClient(base_url=srv.url, timeout=5)

        h = await mgr.headers(factory)
        assert h["Authorization"] == "Bearer t-ok" and srv.logins == 1
    finally:
        srv.close()
