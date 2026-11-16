"""Credential Manager cookie/session：form 登录 → PHPSESSID → Cookie 头注入。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from aip.connectors.credentials import CredentialManager, MemorySecretStore
from aip.libs.common.errors import ErrorCode, PlatformError


class _PhpSessionServer:
    """仿老 PHP 报修：form POST login_do → Set-Cookie PHPSESSID + {"code":1}。"""

    def __init__(self) -> None:
        self.logins = 0
        self.valid: set[str] = set()
        self.require_ok_body = True
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                path = urlparse(self.path).path
                if path != "/Index/login/login_do.html":
                    self._json(404, {"code": 0})
                    return
                form = parse_qs(raw, keep_blank_values=True)
                user = (form.get("user_name") or [""])[0]
                password = (form.get("password") or [""])[0]
                if user != "admin@szgroup.com" or password != "admin.123":
                    self._json(200, {"code": 0})
                    return
                outer.logins += 1
                sid = f"sess{outer.logins}"
                outer.valid.add(sid)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"PHPSESSID={sid}; Max-Age=7200; path=/; HttpOnly",
                )
                body = b'{"code":1}' if outer.require_ok_body else b'{"code":0}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/index/welcome/data_report.html":
                    self._json(404, {"msg": "no"})
                    return
                cookie = self.headers.get("Cookie", "")
                ok = any(f"PHPSESSID={s}" in cookie for s in outer.valid)
                if not ok:
                    self._json(401, {"msg": "unauthorized"})
                    return
                self._json(200, {"msg": "success", "data": {"list": [["1"], ["2"]]}})

            def _json(self, status: int, doc: dict[str, Any]) -> None:
                import json

                raw = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._httpd.server_address[1]
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._t.start()

    def close(self) -> None:
        self._httpd.shutdown()


AUTH = {
    "mode": "cookie",
    "path": "/Index/login/login_do.html",
    "body_encoding": "form",
    "username_field": "user_name",
    "password_field": "password",
    "cookie_name": "PHPSESSID",
    "success_when": "$.code == 1",
    "ttl_s": 7200,
}


@pytest.fixture
def php_server() -> Any:
    s = _PhpSessionServer()
    try:
        yield s
    finally:
        s.close()


@pytest.mark.asyncio
async def test_cookie_login_caches_and_injects(php_server: _PhpSessionServer) -> None:
    base = f"http://127.0.0.1:{php_server.port}"
    store = MemorySecretStore({"kv/data/repair/test": {"username": "admin@szgroup.com", "password": "admin.123"}})
    mgr = CredentialManager(AUTH, "kv/data/repair/test", store)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base, timeout=5)

    h1 = await mgr.headers(factory)
    assert h1["Cookie"].startswith("PHPSESSID=sess")
    assert php_server.logins == 1
    h2 = await mgr.headers(factory)
    assert h2 == h1
    assert php_server.logins == 1  # 缓存

    async with factory() as c:
        r = await c.get("/index/welcome/data_report.html", headers=h1)
    assert r.status_code == 200
    assert r.json()["msg"] == "success"


@pytest.mark.asyncio
async def test_cookie_login_rejects_business_failure(php_server: _PhpSessionServer) -> None:
    php_server.require_ok_body = False
    base = f"http://127.0.0.1:{php_server.port}"
    store = MemorySecretStore({"kv/data/repair/test": {"username": "admin@szgroup.com", "password": "admin.123"}})
    mgr = CredentialManager(AUTH, "kv/data/repair/test", store)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base, timeout=5)

    with pytest.raises(PlatformError) as ei:
        await mgr.headers(factory)
    assert ei.value.code == ErrorCode.DOWNSTREAM_AUTH_EXPIRED


@pytest.mark.asyncio
async def test_cookie_relogin_after_invalidate(php_server: _PhpSessionServer) -> None:
    base = f"http://127.0.0.1:{php_server.port}"
    store = MemorySecretStore({"kv/data/repair/test": {"username": "admin@szgroup.com", "password": "admin.123"}})
    mgr = CredentialManager({**AUTH, "ttl_s": 60}, "kv/data/repair/test", store)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base, timeout=5)

    h1 = await mgr.headers(factory)
    mgr.invalidate(h1["Cookie"])
    h2 = await mgr.headers(factory)
    assert php_server.logins == 2
    assert h1["Cookie"] != h2["Cookie"]


@pytest.mark.asyncio
async def test_cookie_probe_hides_secret(php_server: _PhpSessionServer) -> None:
    base = f"http://127.0.0.1:{php_server.port}"
    store = MemorySecretStore({"kv/data/repair/test": {"username": "admin@szgroup.com", "password": "admin.123"}})
    mgr = CredentialManager(AUTH, "kv/data/repair/test", store)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base, timeout=5)

    out = await mgr.probe(factory)
    assert out["ok"] is True
    assert out["mode"] == "cookie"
    assert out["cookie_name"] == "PHPSESSID"
    assert "PHPSESSID=" not in str(out)
    assert out["expires_in_s"] > 0


@pytest.mark.asyncio
async def test_none_mode_no_secret() -> None:
    mgr = CredentialManager({"mode": "none"}, None, MemorySecretStore())
    assert await mgr.headers(lambda: httpx.AsyncClient()) == {}
    assert (await mgr.probe(lambda: httpx.AsyncClient()))["ok"] is True
