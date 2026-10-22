"""O1：Importer 预览阶段真连接试调（try-operation）。"""

from __future__ import annotations

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from aip.connectors.credentials import MemorySecretStore
from aip.control_plane.app import create_app as create_cp
from aip.control_plane.conn import service as conn_svc
from aip.control_plane.db import Database, DbScope
from aip.control_plane.importer import openapi as oa
from aip.control_plane.importer.draft import build_drafts
from aip.control_plane.importer.try_op import try_operation
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import utcnow
from iam_seed import PASSWORD, Seed, actor_for

ROOT = Path(__file__).resolve().parents[1]
MES = (ROOT / "tests" / "contracts" / "mes-vn01.openapi.yaml").read_text(encoding="utf-8")
SOON = (utcnow() + dt.timedelta(days=30)).isoformat()


class _Handler(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *_: Any) -> None:
        return

    def do_GET(self) -> None:
        type(self).hits += 1
        body = json.dumps(
            {"code": 0, "data": {"items": [{"woNo": "WO-T1", "planQty": 3, "status": "open"}]}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def mock_http() -> Any:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.hits = 0
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()


async def _http_conn(db: Database, seed: Seed, base_url: str) -> Any:
    dev = await actor_for(db, seed, "admin")
    sec = await actor_for(db, seed, "security")
    async with db.tx(DbScope.system()) as conn:
        cid = await conn_svc.create_connection(
            conn,
            dev,
            company_id=seed.vn,
            data={
                "system_key": "mes",
                "env": "test",
                "base_url": base_url,
                "scheme": "http",
                "transport_exception_reason": "O1 try local mock",
                "transport_exception_expires_at": SOON,
            },
        )
        assert await conn_svc.approve_exception(conn, sec, cid) == "active"
        return cid


@pytest.mark.asyncio
async def test_try_operation_live_read(db: Database, seeded: Seed, mock_http: str) -> None:
    cid = await _http_conn(db, seeded, mock_http)
    doc = oa.load_document(MES)
    ops, _ = oa.parse_operations(doc)
    cfg, _ = build_drafts(doc, ops, connector_name="mes_try01", domain="production", system="mes")
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        out = await try_operation(
            conn,
            dev,
            MemorySecretStore(),
            connection_id=cid,
            connector_config=cfg,
            operation="list_work_orders",
            params={"plant": "F001"},
        )
    assert out["ok"] is True
    assert out["mode"] == "live_read"
    assert out["item_count"] >= 1
    assert out["sample"][0]["wo_no"] == "WO-T1"
    assert _Handler.hits >= 1


@pytest.mark.asyncio
async def test_try_write_requires_allow_write(db: Database, seeded: Seed, mock_http: str) -> None:
    cid = await _http_conn(db, seeded, mock_http)
    cfg = {
        "operations": [
            {
                "operation": "create_wo",
                "mode": "write",
                "request": {"method": "POST", "path": "/api/wo", "body": {"plant": "{{ args.plant }}"}},
                "response": {"data": "$"},
            }
        ]
    }
    dev = await actor_for(db, seeded, "admin")
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as e:
            await try_operation(
                conn,
                dev,
                MemorySecretStore(),
                connection_id=cid,
                connector_config=cfg,
                operation="create_wo",
                params={"plant": "F001"},
            )
        assert e.value.code == ErrorCode.SANDBOX_WRITE_BLOCKED


@pytest.mark.asyncio
async def test_try_operation_api(db: Database, seeded: Seed, mock_http: str) -> None:
    cid = await _http_conn(db, seeded, mock_http)
    doc = oa.load_document(MES)
    ops, _ = oa.parse_operations(doc)
    cfg, _ = build_drafts(doc, ops, connector_name="mes_try_api", domain="production", system="mes")
    app = create_cp(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        login = await cp.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": PASSWORD, "totp": seeded.code("admin")},
        )
        assert login.status_code == 200, login.text
        h = {"Authorization": f"Bearer {login.json()['session_token']}"}
        r = await cp.post(
            "/api/v1/importer/try-operation",
            headers=h,
            json={
                "connection_id": str(cid),
                "connector_config": cfg,
                "operation": "list_work_orders",
                "params": {"plant": "F001"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["sample"][0]["wo_no"] == "WO-T1"
