"""M6b：webhook 签名、重试、密钥轮换。

验收：签名可被接收方验证；篡改或过期时间戳被拒；轮换后旧密钥签名验证失败；投递失败会重试。
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from aip.control_plane.db import Database, DbScope
from aip.control_plane.webhook import service as wh
from aip.libs.common.ids import utcnow
from iam_seed import Seed
from test_m1_api import login
from test_m4_gates import _people

pytestmark = pytest.mark.infra


class _Receiver:
    """本地回调：记录请求并校验签名；可配置前 N 次失败后成功。"""

    def __init__(self, secret: str, *, fail_times: int = 0) -> None:
        self.secret = secret
        self.fail_times = fail_times
        self.hits: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        parent = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                ts = self.headers.get(wh.HDR_TS, "")
                nonce = self.headers.get(wh.HDR_NONCE, "")
                sig = self.headers.get(wh.HDR_SIG, "")
                ok = wh.verify_signature(parent.secret, ts, nonce, body, sig)
                with parent._lock:
                    parent.hits.append({"ok": ok, "body": body})
                    n = len(parent.hits)
                if not ok:
                    self.send_response(401)
                    self.end_headers()
                    return
                if n <= parent.fail_times:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_a: Any) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/hook"

    def close(self) -> None:
        self.httpd.shutdown()


def test_sign_verify_rejects_tamper_and_expired() -> None:
    secret = wh.new_secret()
    body = b'{"event":"catalog.changed"}'
    ts = str(int(utcnow().timestamp()))
    nonce = "abc123"
    sig = wh.sign(secret, ts, nonce, body)
    assert wh.verify_signature(secret, ts, nonce, body, sig)
    assert not wh.verify_signature(secret, ts, nonce, b'{"event":"x"}', sig)
    assert not wh.verify_signature("other", ts, nonce, body, sig)
    old = str(int((utcnow() - dt.timedelta(hours=1)).timestamp()))
    old_sig = wh.sign(secret, old, nonce, body)
    assert not wh.verify_signature(secret, old, nonce, body, old_sig)


async def test_deliver_retry_and_rotate(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    recv = _Receiver("pending", fail_times=1)
    try:
        async with db.tx(DbScope.system()) as conn:
            created = await wh.create_subscription(
                conn,
                p.admin,
                consumer_id=seeded.consumers["vn-ai"],
                events=["catalog.changed"],
                url=recv.url,
            )
            recv.secret = created["secret"]
            ep = uuid.UUID(created["endpoint_id"])
            ids = await wh.enqueue(
                conn,
                event_kind="catalog.changed",
                body={"event": "catalog.changed", "payload": {"seq": 1}},
                company_id=seeded.vn,
                event_id="t1",
            )
            assert len(ids) == 1
            r1 = await wh.deliver_one(conn, ids[0])
            assert r1["status"] == "failed" and r1["attempts"] == 1
            await conn.execute(
                text("UPDATE notify.webhook_delivery SET next_retry_at = now() - interval '1 second' WHERE id = :id"),
                {"id": ids[0]},
            )
            r2 = await wh.deliver_one(conn, ids[0])
            assert r2["status"] == "delivered" and r2["attempts"] == 2
            assert len(recv.hits) == 2 and recv.hits[1]["ok"] is True

            old_secret = created["secret"]
            rotated = await wh.rotate_secret(conn, p.admin, ep)
            new_secret = rotated["secret"]
            assert new_secret != old_secret
            body = b'{"x":1}'
            ts = str(int(utcnow().timestamp()))
            nonce = "n1"
            assert not wh.verify_signature(new_secret, ts, nonce, body, wh.sign(old_secret, ts, nonce, body))
            assert wh.verify_signature(new_secret, ts, nonce, body, wh.sign(new_secret, ts, nonce, body))

            recv.secret = new_secret
            recv.fail_times = 0
            ids2 = await wh.enqueue(
                conn,
                event_kind="catalog.changed",
                body={"event": "catalog.changed", "payload": {"seq": 2}},
                company_id=seeded.vn,
            )
            r3 = await wh.deliver_one(conn, ids2[0])
            assert r3["status"] == "delivered"
            assert recv.hits[-1]["ok"] is True
    finally:
        recv.close()


async def test_webhook_http_api(db: Database, seeded: Seed) -> None:
    from aip.control_plane.app import create_app as create_cp
    from iam_seed import app_url

    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        admin = await login(cp, seeded, "admin")
        recv = _Receiver(wh.new_secret(), fail_times=0)
        try:
            cid = str(seeded.consumers["vn-ai"])
            r = await cp.post(
                f"/api/v1/consumers/{cid}/webhooks",
                headers=admin,
                json={"events": ["risk.triggered"], "url": recv.url},
            )
            assert r.status_code == 201, r.text
            body = r.json()
            recv.secret = body["secret"]
            ep = body["endpoint_id"]
            listed = await cp.get(f"/api/v1/consumers/{cid}/webhooks", headers=admin)
            assert listed.status_code == 200 and len(listed.json()["items"]) >= 1
            rot = await cp.post(f"/api/v1/webhooks/{ep}/rotate-secret", headers=admin)
            assert rot.status_code == 200 and rot.json()["secret"] != body["secret"]
            recv.secret = rot.json()["secret"]
            async with db.tx(DbScope.system()) as conn:
                ids = await wh.enqueue(
                    conn,
                    event_kind="risk.triggered",
                    body={"event": "risk.triggered", "payload": {"rule": "x"}},
                    company_id=seeded.vn,
                )
                await wh.deliver_one(conn, ids[0])
            dels = await cp.get(f"/api/v1/webhooks/{ep}/deliveries", headers=admin)
            assert dels.status_code == 200
            assert any(d["status"] == "delivered" for d in dels.json()["items"])
        finally:
            recv.close()
