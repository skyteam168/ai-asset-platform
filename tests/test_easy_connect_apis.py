"""简易接入：凭据入库、测登录、手工导入草稿。"""

from __future__ import annotations

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from aip.connectors.credentials import MemorySecretStore
from aip.control_plane.conn import service as conn_svc
from aip.control_plane.db import Database, DbScope
from aip.control_plane.importer import manual as manual_imp
from aip.libs.common.ids import utcnow
from iam_seed import Seed, actor_for

SOON = (utcnow() + dt.timedelta(days=30)).isoformat()


class _PhpHandler(BaseHTTPRequestHandler):
    logins = 0

    def log_message(self, *_: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        form = parse_qs(raw)
        type(self).logins += 1
        if (form.get("user_name") or [""])[0] != "u1":
            body = b'{"code":0}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"code":1}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "PHPSESSID=abc123; Max-Age=3600; path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/ping":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"msg": "success", "data": {"ok": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def php_http() -> Any:
    _PhpHandler.logins = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PhpHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


async def _conn(db: Database, seed: Seed, base_url: str) -> Any:
    dev = await actor_for(db, seed, "admin")
    sec = await actor_for(db, seed, "security")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_svc.create_connection(
            conn,
            dev,
            company_id=seed.vn,
            data={
                "system_key": "repair",
                "env": "test",
                "base_url": base_url,
                "scheme": "http",
                "transport_exception_reason": "easy connect test",
                "transport_exception_expires_at": SOON,
            },
        )
        assert await conn_svc.approve_exception(conn, sec, cid) == "active"
        return cid


@pytest.mark.asyncio
async def test_put_credentials_and_test_auth(db: Database, seeded: Seed, php_http: str) -> None:
    cid = await _conn(db, seeded, php_http)
    secrets = MemorySecretStore()
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        out = await conn_svc.put_credentials(
            conn, dev, secrets, cid, username="u1", password="p1"
        )
        assert out["credential_ref"] == "kv/data/repair/test"
        row = await conn_svc.row(conn, cid)
        assert row["credential_ref"] == "kv/data/repair/test"
        auth = {
            "mode": "cookie",
            "path": "/Index/login/login_do.html",
            "body_encoding": "form",
            "username_field": "user_name",
            "password_field": "password",
            "cookie_name": "PHPSESSID",
            "success_when": "$.code == 1",
        }
        probe = await conn_svc.test_auth(conn, dev, secrets, cid, auth=auth)
        assert probe["ok"] is True
        assert probe["cookie_name"] == "PHPSESSID"
        assert "abc123" not in str(probe)


@pytest.mark.asyncio
async def test_manual_import_preview_and_persist(db: Database, seeded: Seed, php_http: str) -> None:
    cid = await _conn(db, seeded, php_http)
    dev = await actor_for(db, seeded, "admin")
    ops = [
        {
            "operation": "data_report",
            "method": "GET",
            "path": "/index/welcome/data_report.html",
            "success_when": '$.msg == "success"',
            "data": "$.data",
            "title": "报修看板数据",
            "description": "欢迎页报修统计列表，只读接口用于演示简易接入",
        }
    ]
    async with db.tx(DbScope.system()) as conn:
        preview = await manual_imp.import_manual(
            conn,
            dev,
            connector_name="repair_demo",
            domain="facility",
            system="repair",
            operations=ops,
            connection_id=cid,
            auth={"mode": "none"},
            persist=False,
        )
        assert preview["connector_instance_id"] is None
        assert preview["tools"][0]["capability_id"] == "facility.repair.data_report"
        persisted = await manual_imp.import_manual(
            conn,
            dev,
            connector_name="repair_demo",
            domain="facility",
            system="repair",
            operations=ops,
            connection_id=cid,
            auth={"mode": "none"},
            persist=True,
        )
        assert persisted["connector_instance_id"]
        assert len(persisted["drafts"]) == 1
        assert persisted["drafts"][0]["state"] == "draft"
