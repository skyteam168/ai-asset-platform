"""M5 验收：杀掉 Worker 后 Run 可恢复（真实进程，kill -9 / TerminateProcess）。

- 数据面作为独立进程运行（uvicorn），Worker 在进程内；从磁盘恢复已签名的快照，运行表在测试库。
- 下游 ERP 是测试进程里的 HTTP 服务：收到写请求后先记下幂等键，再拖延 3 秒才响应。
- 第一个数据面在写请求“下游已生效、平台尚未记下结果”时被杀；第二个数据面（不同实例 id）
  在租约过期后接手：执行记录停在 running → 判定陈旧转 unknown → 用幂等键对账 → 确认已执行 → 继续。
- 断言：Run 成功、接手的是第二个实例、下游只收到一次写请求。
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aip.control_plane.db import Database
from iam_seed import app_url
from m3_world import KEY, World, connection, connector, write_tool

pytestmark = pytest.mark.infra
ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = "aipk_m5_crash_token"
SUBMIT, CANCEL = "finance.erp.submit_order", "finance.erp.cancel_order"
WF = "agent.finance.submit_and_finish"


class Erp:
    """假 ERP：POST /orders 记录幂等键后拖延；GET /orders/check?key= 供对账查询。"""

    def __init__(self, hold_s: float) -> None:
        self.posts: list[str] = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with outer.lock:
                    outer.posts.append(self.headers.get("Idempotency-Key", ""))
                time.sleep(hold_s)
                try:
                    self._send({"code": 0, "data": {"order_no": "SO-1"}})
                except OSError:
                    pass  # 调用方已经死了

            def do_GET(self) -> None:
                key = parse_qs(urlparse(self.path).query).get("key", [""])[0]
                with outer.lock:
                    found = key in outer.posts
                self._send({"code": 0, "data": {"found": found, "order_no": "SO-1" if found else None}})

            def log_message(self, *a: object) -> None:
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self) -> None:
        self.srv.shutdown()


DECL = {
    "operations": [
        {
            "connector": "erp",
            "operation": "submit",
            "mode": "write",
            "request": {"method": "POST", "path": "/orders", "body": {"po": "{{ args.po }}"}},
            "response": {"success_when": "$.code == 0", "data": "$.data"},
            "reconcile": {"operation": "check", "found_field": "found"},
        },
        {
            "connector": "erp",
            "operation": "check",
            "request": {"method": "GET", "path": "/orders/check", "query": {"key": "{{ args.idempotency_key }}"}},
            "response": {"success_when": "$.code == 0", "data": "$.data"},
        },
        {
            "connector": "erp",
            "operation": "cancel",
            "mode": "write",
            "request": {"method": "POST", "path": "/orders", "body": {"cancel": "{{ args.po }}"}},
            "response": {"data": "$.data"},
        },
    ]
}


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def erp() -> Iterator[Erp]:
    e = Erp(hold_s=3.0)
    yield e
    e.stop()


def _env(tmp: pathlib.Path, instance: str) -> dict[str, str]:
    pem = KEY.private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "AIP_SIGNING_KEY_PEM": pem,
        "AIP_DATA_DIR": str(tmp),
        "AIP_RUNTIME_DATABASE_URL": app_url(),
        "AIP_AUDIT_DATABASE_URL": "",
        "AIP_CONTROL_PLANE_URL": "",
        "AIP_INSTANCE_ID": instance,
        "AIP_WORKFLOW_LEASE_S": "2",
        "AIP_WORKFLOW_STALE_S": "1",
        "AIP_WORKFLOW_SYNC_WAIT_S": "0",
        "AIP_RUNTIME_MODE": "sandbox",
    }


def _launch(tmp: pathlib.Path, instance: str, port: int) -> subprocess.Popen[bytes]:
    log = (tmp / f"{instance}.log").open("wb")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aip.data_plane.app:app", "--port", str(port), "--app-dir", "src"],
        cwd=ROOT,
        env=_env(tmp, instance),
        stdout=log,
        stderr=subprocess.STDOUT,
    )


async def _spawn(tmp: pathlib.Path, instance: str) -> tuple[subprocess.Popen[bytes], str]:
    port = _port()
    proc = _launch(tmp, instance, port)
    url = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                if (await c.get(f"{url}/readyz")).status_code == 200:
                    return proc, url
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    proc.kill()
    raise AssertionError((tmp / f"{instance}.log").read_text(encoding="utf-8", errors="replace")[-3000:])


async def test_kill_worker_mid_write_then_recover(db: Database, tmp_path: pathlib.Path, erp: Erp) -> None:
    flow = {
        "name": "submit_and_finish",
        "capability_id": WF,
        "version": "1.0.0",
        "input_schema": {"type": "object"},
        "budget": {"max_steps": 10, "max_llm_calls": 0, "timeout_s": 300},
        "nodes": [
            {"id": "submit", "type": "tool", "ref": f"{SUBMIT}@1.0.0", "args": {"po": "$input.po"},
             "compensation": f"{CANCEL}@1.0.0", "compensation_args": {"po": "$input.po"}, "next": "done"},
            {"id": "done", "type": "end", "output": {"order": "$submit.order_no"}},
        ],
    }  # fmt: skip
    common = {"input_schema": {"type": "object"}, "scope_binding": {}, "app_identity_allowed": True}
    timeout = {"connect_ms": 1000, "read_ms": 6000, "deadline_ms": 6000}
    tools = [
        write_tool(SUBMIT, "erp", "submit", timeout=timeout, **common),
        write_tool(CANCEL, "erp", "cancel", **common),
    ]  # fmt: skip
    # 在测试进程里构造并签名快照：create_app 会把它们落盘到 tmp/snapshots，子进程启动时从磁盘恢复
    engine = create_async_engine(app_url())
    World(tmp_path, tools, [connector("erp", "declarative", DECL, connection(erp.url))], workflows=[flow],
          runtime_engine=engine, static_token=STATIC)  # fmt: skip
    await engine.dispose()

    dp1, url1 = await _spawn(tmp_path, "dp-crash-1")
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{url1}/api/v1/invoke/{WF}",
                json={"args": {"po": "PO-K9"}},
                headers={"Authorization": f"Bearer {STATIC}", "Idempotency-Key": "k9"},
            )
            assert r.status_code == 200, r.text
            run_id = r.json()["run_id"]
        for _ in range(100):  # 等到下游已收到写请求（并开始拖延）
            if erp.posts:
                break
            await asyncio.sleep(0.05)
        assert erp.posts == [f"wf:{run_id}:submit"]
    finally:
        dp1.kill()  # Windows 上是 TerminateProcess，等同 kill -9：不释放租约，不写任何收尾
        dp1.wait()

    t0 = time.monotonic()
    dp2, url2 = await _spawn(tmp_path, "dp-crash-2")
    try:
        status: dict[str, Any] = {}
        async with httpx.AsyncClient() as c:
            for _ in range(200):
                status = (
                    await c.get(f"{url2}/api/v1/runs/{run_id}", headers={"Authorization": f"Bearer {STATIC}"})
                ).json()
                if status["status"] in ("succeeded", "failed", "compensated"):
                    break
                await asyncio.sleep(0.1)
        recovered_in = time.monotonic() - t0
    finally:
        dp2.kill()
        dp2.wait()
    assert status["status"] == "succeeded", status
    assert status["result"] == {"order": "SO-1"}
    assert {s["node_id"]: s["executor"] for s in status["steps"]}["submit"] == "dp-crash-2"
    assert erp.posts == [f"wf:{run_id}:submit"]  # 下游只收到一次写请求
    engine = create_async_engine(app_url())
    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
        rec = (
            await conn.execute(
                text("SELECT status, reconcile_status FROM exec.tool_execution WHERE idempotency_key = :k"),
                {"k": f"wf:{run_id}:submit"},
            )
        ).one()
    await engine.dispose()
    assert tuple(rec) == ("succeeded", "confirmed")
    print(f"\n[M5 crash] 第二个实例接手并完成：{recovered_in:.1f}s（含进程启动、租约 2s、陈旧判定 1s）")
