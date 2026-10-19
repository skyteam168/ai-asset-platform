"""M3 / P3：Tool Runtime、声明式 Connector、Credential Manager（architecture.md 8、9）。

验收：超时、熔断、幂等重放、沙箱拒写、unknown 对账、并发刷新只登录一次、TLS 错误码贯通。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from aip.connectors.mock import MockConnector
from aip.libs.httpclient.certs import fp_cert
from m3_world import CONSUMER, World, connection, connector, tool, write_tool
from tls_fixtures import Server, make_cert

pytestmark = pytest.mark.infra
CAP = "production.mes.query_work_orders"
ARGS = {"factory_id": "F001", "status": "open"}

DECL = {
    "operations": [
        {
            "connector": "mes",
            "operation": "query_work_orders",
            "request": {
                "method": "GET",
                "path": "/api/v2/workorders",
                "query": {"plant": "{{ args.factory_id }}", "status": "{{ args.status | default('') }}"},
            },
            "response": {
                "success_when": "$.code == 0",
                "data": "$.data.items[*]",
                "fields": {"wo_no": "$.woNo", "qty": "$.planQty", "status": "$.status", "secret": "$.woNo"},
            },
            "errors": [{"when": "$.code == 5003", "map": "BUSINESS_RULE_VIOLATION"}],
        }
    ]
}


def mock(name: str = "mock", **ops: dict[str, Any]) -> dict[str, Any]:
    return connector(name, "mock", {"operations": ops})


# ---------------- 声明式 Connector 走真实 HTTPS（自签 + pinned） ----------------


async def test_declarative_over_pinned_https(tmp_path: pathlib.Path) -> None:
    cert = make_cert()
    srv = Server(cert)
    try:
        conn = connection(srv.url(), tls_mode="pinned", pins=[{"kind": "cert", "sha256": fp_cert(cert.der)}])
        w = World(tmp_path, [tool(CAP, "mes", "query_work_orders")], [connector("mes", "declarative", DECL, conn)])
        await w.ready()
        r = await w.runtime.invoke(w.principal(), CAP, ARGS)
        assert r["status"] == "succeeded", r
        # 映射后的字段 + Result Guard 按 output_schema 裁掉未声明的 secret 字段
        assert r["data"] == [{"wo_no": "WO-1", "qty": 10, "status": "open"}]
        rec = await w.store.get(r["execution_id"])
        assert rec["transport"] == "https" and rec["tls_verified"] is True
    finally:
        srv.stop()


async def test_tls_failure_surfaces_precise_code_through_runtime(tmp_path: pathlib.Path) -> None:
    srv = Server(make_cert())
    try:
        conn = connection(srv.url(), tls_mode="pinned", pins=[{"kind": "cert", "sha256": "00" * 32}])
        w = World(tmp_path, [tool(CAP, "mes", "query_work_orders")], [connector("mes", "declarative", DECL, conn)])
        await w.ready()
        r = await w.runtime.invoke(w.principal(), CAP, ARGS)
        assert r["status"] == "failed" and r["error"]["code"] == "TLS_PIN_MISMATCH"
        assert srv.hits == 0
    finally:
        srv.stop()


async def test_rest_invoke_endpoint(tmp_path: pathlib.Path) -> None:
    w = World(tmp_path, [tool(CAP, "mock", "q")], [mock(q={"response": [{"wo_no": "W1", "qty": 1}]})])
    await w.ready()
    from aip.libs.tokens.secrets_util import token_hash

    w.app.state.iam.data["consumers"][CONSUMER]["static_tokens"] = {
        token_hash("aipk_test"): {"expires_at": "2099-01-01T00:00:00+00:00", "ip_binding": []}
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="https://dp") as c:
        r = await c.post(f"/api/v1/invoke/{CAP}", json={"args": ARGS}, headers={"Authorization": "Bearer aipk_test"})
        assert r.status_code == 200 and r.json()["status"] == "succeeded", r.text
        got = await c.get(
            f"/api/v1/executions/{r.json()['execution_id']}", headers={"Authorization": "Bearer aipk_test"}
        )
        assert got.json()["status"] == "succeeded"
        bad = await c.post(f"/api/v1/invoke/{CAP}", json={"args": {}}, headers={"Authorization": "Bearer aipk_test"})
        assert bad.json()["error"]["code"] == "INVALID_ARGUMENT" and bad.json()["next_action"]["type"] == "fix_args"


# ---------------- 超时、重试、熔断、限流 ----------------


async def test_timeout(tmp_path: pathlib.Path) -> None:
    w = World(
        tmp_path,
        [tool(CAP, "mock", "q", retry_class={"DOWNSTREAM_TIMEOUT": {"max_attempts": 0}})],
        [mock(q={"response": [], "delay_s": 5})],
    )
    await w.ready()
    r = await w.runtime.invoke(w.principal(), CAP, ARGS)
    assert r["status"] == "failed" and r["error"]["code"] == "DOWNSTREAM_TIMEOUT" and r["error"]["retryable"]


async def test_retry_transient_failure_for_reads(tmp_path: pathlib.Path) -> None:
    w = World(tmp_path, [tool(CAP, "mock", "q")], [mock(q={"response": [{"wo_no": "W"}], "fail_times": 1})])
    await w.ready()
    r = await w.runtime.invoke(w.principal(), CAP, ARGS)
    assert r["status"] == "succeeded"
    assert (await w.store.get(r["execution_id"]))["retry_count"] == 1


async def test_circuit_breaker_opens_after_consecutive_failures(tmp_path: pathlib.Path) -> None:
    w = World(
        tmp_path,
        [tool(CAP, "mock", "q", retry_class={"DOWNSTREAM_UNAVAILABLE": {"max_attempts": 0}})],
        [mock(q={"response": [], "fail_times": 100})],
    )
    await w.ready()
    codes = [(await w.runtime.invoke(w.principal(), CAP, ARGS))["error"]["code"] for _ in range(7)]
    assert codes[:5] == ["DOWNSTREAM_UNAVAILABLE"] * 5
    assert codes[5:] == ["CIRCUIT_OPEN", "CIRCUIT_OPEN"]
    assert w.connector("mock").calls["q"] == 5, "熔断后不再打下游"


async def test_rate_limit(tmp_path: pathlib.Path) -> None:
    w = World(
        tmp_path,
        [tool(CAP, "mock", "q")],
        [connector("mock", "mock", {"operations": {"q": {"response": []}}}, rate_per_s=0.001, burst=2)],
    )
    await w.ready()
    codes = [(await w.runtime.invoke(w.principal(), CAP, ARGS))["status"] for _ in range(3)]
    assert codes == ["succeeded", "succeeded", "failed"]


# ---------------- 幂等、沙箱拒写、unknown 对账 ----------------


async def test_idempotent_replay_and_conflict(tmp_path: pathlib.Path) -> None:
    w = World(
        tmp_path,
        [write_tool("production.mes.report_output", "mock", "w")],
        [mock(w={"response": {"ok": True}, "write": True}, query={"response": {}})],
    )
    await w.ready()
    p = w.principal()
    first = await w.runtime.invoke(p, "production.mes.report_output", ARGS, idempotency_key="k-1")
    again = await w.runtime.invoke(p, "production.mes.report_output", ARGS, idempotency_key="k-1")
    assert first["status"] == "succeeded" and again["execution_id"] == first["execution_id"]
    assert w.connector("mock").calls["w"] == 1, "同键重放不再调用下游"
    conflict = await w.runtime.invoke(p, "production.mes.report_output", {**ARGS, "status": "x"}, idempotency_key="k-1")
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    missing = await w.runtime.invoke(p, "production.mes.report_output", ARGS)
    assert missing["error"]["code"] == "INVALID_ARGUMENT"


async def test_sandbox_blocks_live_writes(tmp_path: pathlib.Path) -> None:
    srv = Server()
    try:
        write_map = {
            "operations": [
                {
                    "connector": "mes",
                    "operation": "report",
                    "mode": "write",
                    "request": {"method": "POST", "path": "/api/report", "body": {"n": 1}},
                    "response": {"data": "$"},
                }
            ]
        }
        prod = World(
            tmp_path / "a",
            [write_tool("production.mes.report_output", "mes", "report")],
            [connector("mes", "declarative", write_map, connection(srv.url(), env="prod"))],
        )
        await prod.ready()
        r = await prod.runtime.invoke(prod.principal(), "production.mes.report_output", ARGS, idempotency_key="s1")
        assert r["error"]["code"] == "SANDBOX_WRITE_BLOCKED"
        assert srv.hits == 0
    finally:
        srv.stop()


async def test_write_timeout_goes_unknown_then_reconciles(tmp_path: pathlib.Path) -> None:
    MockConnector.executed.clear()
    w = World(
        tmp_path,
        [write_tool("production.mes.report_output", "mock", "w")],
        [mock(w={"response": {"ok": True}, "write": True, "commit_then_hang": True}, query={"response": {}})],
    )
    await w.ready()
    r = await w.runtime.invoke(w.principal(), "production.mes.report_output", ARGS, idempotency_key="u-1")
    assert r["status"] == "unknown" and r["next_action"]["type"] == "reconcile"
    assert w.connector("mock").calls["w"] == 1, "结果未知的写操作不自动重放"
    again = await w.runtime.invoke(w.principal(), "production.mes.report_output", ARGS, idempotency_key="u-1")
    assert again["status"] == "unknown"
    done = await w.runtime.reconcile(r["execution_id"])
    assert done["status"] == "succeeded" and done["data"] == {"ok": True}


async def test_reconcile_not_executed(tmp_path: pathlib.Path) -> None:
    MockConnector.executed.clear()
    w = World(
        tmp_path,
        [write_tool("production.mes.report_output", "mock", "w")],
        [mock(w={"response": {}, "write": True, "delay_s": 5}, query={"response": {}})],
    )
    await w.ready()
    r = await w.runtime.invoke(w.principal(), "production.mes.report_output", ARGS, idempotency_key="u-2")
    assert r["status"] == "unknown"
    MockConnector.executed.clear()  # 下游实际上没有落库
    assert (await w.runtime.reconcile(r["execution_id"]))["status"] == "failed"


# ---------------- PDP 贯通、Result Guard ----------------


async def test_policy_denial_and_confirmation(tmp_path: pathlib.Path) -> None:
    w = World(
        tmp_path,
        [tool(CAP, "mock", "q"), write_tool("production.mes.close_order", "mock", "w", confirm_policy="user_confirm")],
        [mock(q={"response": []}, w={"response": {}, "write": True}, query={"response": {}})],
    )
    await w.ready()
    other_factory = await w.runtime.invoke(w.principal(), CAP, {"factory_id": "F900"})
    assert other_factory["error"]["code"] == "OUT_OF_SCOPE"
    assert other_factory["next_action"]["type"] == "request_permission"
    confirm = await w.runtime.invoke(w.principal(), "production.mes.close_order", ARGS, idempotency_key="c1")
    assert confirm["status"] == "pending_confirm" and confirm["next_action"]["type"] == "confirm"
    assert w.connector("mock").calls.get("w") is None


async def test_result_guard_masks_pii_and_truncates(tmp_path: pathlib.Path) -> None:
    rows = [{"wo_no": f"W{i}", "qty": i, "phone": "0901234567", "internal_cost": 99} for i in range(30)]
    spec = tool(CAP, "mock", "q", pii_fields=["[*].phone"], output_limits={"max_items": 10})
    w = World(tmp_path, [spec], [mock(q={"response": rows})])
    await w.ready()
    r = await w.runtime.invoke(w.principal(), CAP, ARGS)
    assert len(r["data"]) == 10 and r["page"]["truncated"] is True
    assert r["data"][0] == {"wo_no": "W0", "qty": 0, "phone": "***"}
    w3 = World(tmp_path / "l3", [spec], [mock(q={"response": rows})], clearance=3)
    await w3.ready()
    assert (await w3.runtime.invoke(w3.principal(), CAP, ARGS))["data"][0]["phone"] == "0901234567"


# ---------------- Credential Manager：并发刷新只登录一次，401 刷新后重试一次 ----------------


class LoginServer:
    def __init__(self) -> None:
        self.logins = 0
        self.valid: set[str] = set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.logins += 1
                token = f"tok-{outer.logins}"
                outer.valid = {token}
                self._json(200, {"data": {"token": token}})

            def do_GET(self) -> None:
                if self.headers.get("Authorization", "").removeprefix("Bearer ") not in outer.valid:
                    self._json(401, {"code": 401})
                    return
                self._json(200, {"code": 0, "data": {"items": [{"woNo": "W", "planQty": 1, "status": "open"}]}})

            def _json(self, status: int, doc: dict[str, Any]) -> None:
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


async def test_concurrent_calls_login_once_and_401_refreshes_once(tmp_path: pathlib.Path) -> None:
    srv = LoginServer()
    try:
        cfg = {**DECL, "auth": {"mode": "login", "path": "/api/login", "token_path": "$.data.token", "ttl_s": 600}}
        conn = connection(srv.url, credential_ref="kv/data/mes")
        spec = tool(CAP, "mes", "query_work_orders", timeout={"connect_ms": 2000, "read_ms": 5000, "deadline_ms": 8000})
        w = World(
            tmp_path,
            [spec],
            [connector("mes", "declarative", cfg, conn)],
            secrets={"kv/data/mes": {"username": "svc_aip", "password": "pw"}},
        )
        await w.ready()
        results = await asyncio.gather(*[w.runtime.invoke(w.principal(), CAP, ARGS) for _ in range(10)])
        assert all(r["status"] == "succeeded" for r in results), results[0]
        assert srv.logins == 1, "并发刷新只登录一次"
        srv.valid = set()  # 下游令牌失效 → 401 → 刷新一次后重试成功
        r = await w.runtime.invoke(w.principal(), CAP, ARGS)
        assert r["status"] == "succeeded" and srv.logins == 2
    finally:
        srv.httpd.shutdown()


async def test_declarative_business_code_failure(tmp_path: pathlib.Path) -> None:
    class Biz(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"code":5003,"msg":"plant closed"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Biz)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        w = World(
            tmp_path,
            [tool(CAP, "mes", "query_work_orders")],
            [connector("mes", "declarative", DECL, connection(f"http://127.0.0.1:{httpd.server_address[1]}"))],
        )
        await w.ready()
        r = await w.runtime.invoke(w.principal(), CAP, ARGS)
        assert r["status"] == "failed" and r["error"]["code"] == "BUSINESS_RULE_VIOLATION"
        assert "plant closed" not in json.dumps(r), "不向调用方泄露下游内部信息"
    finally:
        httpd.shutdown()


async def test_optional_template_arguments_use_defaults(tmp_path: pathlib.Path) -> None:
    """未传的可选参数走 default 过滤器；必需参数缺失才报映射失败。"""
    from aip.connectors.base import ExecContext
    from aip.connectors.declarative import _env, _render

    env = _env(ExecContext(trace_id="t", deadline=0, mode="sandbox"))
    assert _render(env, "{{ args.limit | default(5) | max(20) }}", {"args": {}}) == "5"
    assert _render(env, "{{ args.limit | default(5) | max(20) }}", {"args": {"limit": 500}}) == "20"
    from aip.libs.common.errors import PlatformError

    with pytest.raises(PlatformError):
        _render(env, "{{ args.plant }}", {"args": {}})
    with pytest.raises(PlatformError):  # 沙箱：不能访问对象内部属性
        _render(env, "{{ args.__class__.__mro__ }}", {"args": {}})
