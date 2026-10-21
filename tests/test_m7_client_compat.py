"""M7h：MCP 客户端兼容冒烟（architecture.md 10.1）。

至少 3 个客户端画像 × 双协议覆盖：
1. 官方 MCP Python SDK（legacy + 2026-07-28）
2. MCP Inspector 画像（现代协议 + Mcp-Method / Mcp-Name）
3. 桌面 Agent 画像（2025-11-25 initialize 后无 _meta 版本调用）

矩阵文档：docs/ops/client-compat.md
"""

from __future__ import annotations

import asyncio
import datetime as dt
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

from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from aip.libs.tokens.secrets_util import token_hash
from m3_world import CONSUMER, World, connector, tool

pytestmark = pytest.mark.infra

CAP = "production.mes.query_work_orders"
SECRET = "finance.erp.salary_summary"
ROWS = [{"wo_no": "WO-compat", "qty": 1, "status": "open"}]
CONSUMER_KEY = SigningKey.generate()
STATIC = "aipk_m7h_compat"
LEGACY, MODERN = "2025-11-25", "2026-07-28"


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


class Live:
    def __init__(self, world: World, base: str, mcp_url: str, access_token: str) -> None:
        self.world, self.base, self.url, self.access_token = world, base, mcp_url, access_token

    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


@pytest.fixture
async def live(tmp_path: pathlib.Path) -> AsyncIterator[Live]:
    w = World(
        tmp_path,
        [
            tool(CAP, "mock", "q"),
            tool(SECRET, "mock", "q", data_classification=4),
        ],
        [connector("mock", "mock", {"operations": {"q": {"response": ROWS}}})],
        consumer_jwk=CONSUMER_KEY.public_jwk(),
        static_token=STATIC,
        exposure=["rest", "mcp"],
    )
    w.app.state.iam.data["consumers"][CONSUMER]["static_tokens"] = {
        token_hash(STATIC): {"expires_at": "2099-01-01T00:00:00+00:00", "ip_binding": []}
    }
    await w.ready()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(w.app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{base}/oauth/token",
            headers={"Authorization": f"Bearer {STATIC}"},
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion()},
        )
        assert r.status_code == 200, r.text
        tok = r.json()["access_token"]
    yield Live(w, base, f"{base}/mcp", tok)
    server.should_exit = True
    await task


def _modern_rpc(method: str, params: dict[str, Any] | None = None, *, rid: int = 1) -> dict[str, Any]:
    p = dict(params or {})
    p["_meta"] = {"io.modelcontextprotocol/protocolVersion": MODERN}
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": p}


def _assert_list_and_call_result(names: set[str], call_body: dict[str, Any]) -> None:
    assert CAP in names
    assert SECRET not in names
    assert "aip.get_run_status" in names
    assert call_body.get("isError") is False
    structured = call_body.get("structuredContent") or {}
    assert structured.get("items") == ROWS or structured == {"items": ROWS}


@pytest.mark.parametrize("mode", ["legacy", MODERN])
async def test_official_sdk_smoke(live: Live, mode: str) -> None:
    """客户端 1：官方 MCP Python SDK（双协议）。"""
    client = Client(
        streamable_http_client(live.url, http_client=create_mcp_http_client(headers=live.bearer())),
        mode=mode,
    )
    async with client:
        expected = LEGACY if mode == "legacy" else MODERN
        assert client.protocol_version == expected
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        res = await client.call_tool(CAP, {"factory_id": "F001"})
        _assert_list_and_call_result(
            names,
            {
                "isError": res.is_error,
                "structuredContent": res.structured_content,
            },
        )


async def test_inspector_http_smoke(live: Live) -> None:
    """客户端 2：MCP Inspector 画像——现代协议 + 路由头。"""
    async with httpx.AsyncClient(timeout=15) as c:
        anon = await c.post(live.url, json=_modern_rpc("tools/list"))
        assert anon.status_code == 401
        assert "resource_metadata" in anon.headers.get("www-authenticate", "")

        h = {**live.bearer(), "Mcp-Method": "tools/list"}
        listed = await c.post(live.url, json=_modern_rpc("tools/list"), headers=h)
        assert listed.status_code == 200, listed.text
        body = listed.json()["result"]
        assert body.get("cacheScope") == "private"
        names = {t["name"] for t in body["tools"]}

        call_headers = {**live.bearer(), "Mcp-Method": "tools/call", "Mcp-Name": CAP}
        called = await c.post(
            live.url,
            json=_modern_rpc("tools/call", {"name": CAP, "arguments": {"factory_id": "F001"}}),
            headers=call_headers,
        )
        assert called.status_code == 200, called.text
        _assert_list_and_call_result(names, called.json()["result"])


async def test_desktop_legacy_http_smoke(live: Live) -> None:
    """客户端 3：桌面 Agent 画像——legacy initialize 后无现代 _meta。"""
    async with httpx.AsyncClient(timeout=15) as c:
        init = await c.post(
            live.url,
            headers=live.bearer(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LEGACY,
                    "capabilities": {},
                    "clientInfo": {"name": "desktop-compat", "version": "0.0.0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        assert init.json()["result"]["protocolVersion"] == LEGACY

        # 通知（无 id）→ 202
        note = await c.post(
            live.url,
            headers=live.bearer(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert note.status_code == 202

        listed = await c.post(
            live.url,
            headers=live.bearer(),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed.status_code == 200, listed.text
        names = {t["name"] for t in listed.json()["result"]["tools"]}

        called = await c.post(
            live.url,
            headers=live.bearer(),
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": CAP, "arguments": {"factory_id": "F001"}},
            },
        )
        assert called.status_code == 200, called.text
        _assert_list_and_call_result(names, called.json()["result"])
