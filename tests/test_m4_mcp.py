"""M4 / P5：MCP Gateway。官方 MCP Python SDK 客户端分别以 2025-11-25（legacy 握手）与 2026-07-28（无状态）连接。

验收：同一调用经 MCP（两个版本）与 REST 结果与审计一致；未授权工具不出现在 tools/list。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import secrets
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError

from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.secrets_util import token_hash
from m3_world import CONSUMER, World, connector, tool, write_tool

pytestmark = pytest.mark.infra
CAP = "production.mes.query_work_orders"
WRITE = "production.mes.report_output"
SECRET_TOOL = "finance.erp.salary_summary"
REST_ONLY = "production.mes.export_all"
ROWS = [{"wo_no": "WO-1", "qty": 10, "status": "open"}]
CONSUMER_KEY = SigningKey.generate()
STATIC = "aipk_mcp_test_token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def assertion(sub: str = "emp-1") -> str:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    return jwt.encode(
        {
            "iss": "vn-ai",
            "sub": sub,
            "aud": get_settings().mcp_resource,
            "iat": now,
            "exp": now + 120,
            "jti": secrets.token_hex(8),
        },
        CONSUMER_KEY.private,
        algorithm="ES256",
        headers={"kid": CONSUMER_KEY.kid},
    )


def headers() -> dict[str, str]:
    """请求头断言（模式 B）：每个请求必须是新断言（jti 防重放）。"""
    return {"Authorization": f"Bearer {STATIC}", "X-User-Assertion": assertion()}


class Live:
    def __init__(self, world: World, url: str) -> None:
        self.world, self.url = world, url
        self.access_token = ""

    def bearer(self) -> dict[str, str]:
        """MCP 客户端会在每个请求上复用同一组头，所以用令牌交换（模式 A）得到的平台访问令牌。"""
        return {"Authorization": f"Bearer {self.access_token}"}


@pytest.fixture
async def live(tmp_path: pathlib.Path) -> AsyncIterator[Live]:
    tools = [
        tool(CAP, "mock", "q"),
        write_tool(WRITE, "mock", "w"),
        tool(SECRET_TOOL, "mock", "q", data_classification=4),  # 用户 L2：无权
    ]
    w = World(
        tmp_path,
        tools,
        [
            connector(
                "mock",
                "mock",
                {
                    "operations": {
                        "q": {"response": ROWS},
                        "w": {"response": {"ok": True}, "write": True},
                        "query": {"response": {}},
                    }
                },
            )
        ],
        consumer_jwk=CONSUMER_KEY.public_jwk(),
    )
    # 只开放 REST 的工具：单独发布，不应出现在 MCP tools/list
    rel = dict(w.app.state.releases.data["releases"][0])
    w.app.state.releases.data["releases"].append(
        {
            **rel,
            "capability_id": REST_ONLY,
            "name": REST_ONLY,
            "exposure": ["rest"],
            "spec": {**rel["spec"], "capability_id": REST_ONLY},
        }
    )
    w.app.state.releases._on_load(w.app.state.releases.data)
    w.app.state.iam.data["consumers"][CONSUMER]["static_tokens"] = {
        token_hash(STATIC): {"expires_at": "2099-01-01T00:00:00+00:00", "ip_binding": []}
    }
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(w.app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    live = Live(w, f"http://127.0.0.1:{port}/mcp")
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"http://127.0.0.1:{port}/oauth/token",
            headers={"Authorization": f"Bearer {STATIC}"},
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion()},
        )
        assert r.status_code == 200, r.text
        live.access_token = r.json()["access_token"]
    yield live
    server.should_exit = True
    await task


def sdk(live: Live, mode: str) -> Client:
    return Client(
        streamable_http_client(live.url, http_client=create_mcp_http_client(headers=live.bearer())), mode=mode
    )


# ---------------- 官方 SDK 客户端：两个协议版本 ----------------


@pytest.mark.parametrize(
    ("mode", "expected_version"), [("legacy", "2025-11-25"), ("2026-07-28", "2026-07-28"), ("auto", "2026-07-28")]
)
async def test_sdk_client_both_protocol_versions(live: Live, mode: str, expected_version: str) -> None:
    async with sdk(live, mode) as client:
        assert client.protocol_version == expected_version
        listed = await client.list_tools()
        names = [t.name for t in listed.tools]
        assert names == sorted([CAP, WRITE]), "未授权（L4）与只开放 REST 的工具不得出现在 tools/list"
        q = next(t for t in listed.tools if t.name == CAP)
        assert q.annotations is not None and q.annotations.read_only_hint is True
        assert next(t for t in listed.tools if t.name == WRITE).annotations.destructive_hint is True
        res = await client.call_tool(CAP, {"factory_id": "F001"})
        assert res.is_error is False
        assert res.structured_content == {"items": ROWS}
        assert res.meta and res.meta.get("aip.internal/untrusted") is True


async def test_unauthorized_tool_call_rejected_like_unknown(live: Live) -> None:
    """无权的、只开放 REST 的与不存在的工具返回同一个 JSON-RPC 错误，不泄露能力是否存在。"""
    messages = []
    for name in (SECRET_TOOL, "no.such.tool", REST_ONLY):
        async with sdk(live, "2026-07-28") as client:
            with pytest.raises(MCPError) as e:
                await client.call_tool(name, {"factory_id": "F001"})
            messages.append(str(e.value).replace(name, "<tool>"))
    assert messages == ["Unknown tool: <tool>"] * 3


async def test_out_of_scope_argument_is_tool_error(live: Live) -> None:
    async with sdk(live, "legacy") as client:
        res = await client.call_tool(CAP, {"factory_id": "F900"})
    assert res.is_error is True
    assert res.meta["aip.internal/error"]["code"] == "OUT_OF_SCOPE"


# ---------------- 原始协议细节 ----------------


def rpc(
    method: str, params: dict[str, Any] | None = None, version: str | None = "2026-07-28", rid: int = 1
) -> dict[str, Any]:
    p = dict(params or {})
    if version:
        p["_meta"] = {"io.modelcontextprotocol/protocolVersion": version}
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": p}


async def test_protocol_edge_cases(live: Live) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(live.url, json=rpc("tools/list"))
        assert r.status_code == 401
        assert 'resource_metadata="' in r.headers["www-authenticate"]

        h = headers()
        r = await c.post(live.url, json=rpc("tools/list"), headers={**h, "Mcp-Method": "tools/list"})
        body = r.json()["result"]
        assert body["cacheScope"] == "private" and body["ttlMs"] > 0 and body["resultType"] == "complete"
        assert [t["name"] for t in body["tools"]] == sorted([CAP, WRITE])
        assert body["tools"][0]["outputSchema"]["type"] == "object"

        r = await c.post(live.url, json=rpc("server/discover"), headers=headers())
        assert set(r.json()["result"]["supportedVersions"]) >= {"2025-11-25", "2026-07-28"}

        r = await c.post(live.url, json=rpc("tools/list", version="2030-01-01"), headers=headers())
        assert r.status_code == 400 and r.json()["error"]["code"] == -32022
        assert r.json()["error"]["data"]["requested"] == "2030-01-01"

        r = await c.post(live.url, json=rpc("tools/list"), headers={**headers(), "Mcp-Method": "tools/call"})
        assert r.json()["error"]["code"] == -32020

        r = await c.post(
            live.url,
            json=rpc("tools/call", {"name": CAP, "arguments": {"factory_id": "F001"}}),
            headers={**headers(), "Mcp-Method": "tools/call", "Mcp-Name": "other.tool"},
        )
        assert r.json()["error"]["code"] == -32020

        r = await c.post(live.url, json=rpc("resources/list"), headers=headers())
        assert r.json()["error"]["code"] == -32601

        r = await c.post(live.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers())
        assert r.status_code == 202

        r = await c.post(live.url, content=b"{not json", headers=headers())
        assert r.json()["error"]["code"] == -32700

        assert (await c.get(live.url, headers=headers())).status_code == 405


async def test_write_tool_via_mcp_requires_idempotency_and_replays(live: Live) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        call = rpc("tools/call", {"name": WRITE, "arguments": {"factory_id": "F001"}})
        r = await c.post(live.url, json=call, headers=headers())
        assert r.json()["result"]["isError"] is True
        assert r.json()["result"]["_meta"]["aip.internal/error"]["code"] == "INVALID_ARGUMENT"
        first = await c.post(live.url, json=call, headers={**headers(), "Idempotency-Key": "mcp-w-1"})
        again = await c.post(live.url, json=call, headers={**headers(), "Idempotency-Key": "mcp-w-1"})
    a, b = first.json()["result"], again.json()["result"]
    assert a["isError"] is False and a["_meta"]["aip.internal/execution_id"] == b["_meta"]["aip.internal/execution_id"]


# ---------------- MCP 与 REST 一致 ----------------


async def test_mcp_and_rest_results_and_audit_are_consistent(live: Live) -> None:
    args = {"factory_id": "F001", "status": "open"}
    async with sdk(live, "legacy") as legacy:
        r1 = await legacy.call_tool(CAP, args)
    async with sdk(live, "2026-07-28") as modern:
        r2 = await modern.call_tool(CAP, args)
    async with httpx.AsyncClient(timeout=10) as c:
        rest = (
            await c.post(live.url.replace("/mcp", f"/api/v1/invoke/{CAP}"), json={"args": args}, headers=headers())
        ).json()
    assert r1.structured_content == r2.structured_content == {"items": rest["data"]}
    spool = live.world.app.state.audit.dir
    records = [json.loads(line) for p in sorted(spool.glob("*.jsonl")) for line in p.read_text("utf-8").splitlines()]
    decisions = [r["record"] for r in records if r["kind"] == "pdp.decision" and r["record"]["capability_id"] == CAP]
    executions = [r["record"] for r in records if r["kind"] == "tool.execution"]
    assert [d["exposure"] for d in decisions[-3:]] == ["mcp", "mcp", "rest"]
    assert {d["decision"] for d in decisions[-3:]} == {"allow"}
    assert len({d["args_sha256"] for d in decisions[-3:]}) == 1
    assert len({d["end_user_id"] for d in decisions[-3:]}) == 1 and decisions[-1]["end_user_id"]
    assert [e["status"] for e in executions[-3:]] == ["succeeded"] * 3
    assert len({e["args_sha256"] for e in executions[-3:]}) == 1
