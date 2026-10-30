"""G1.2：上游 MCP tools/list 在线拉哈希（preview-hash）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from aip.connectors.upstream_mcp import tools_manifest_hash
from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database
from aip.libs.common.ids import utcnow
from iam_seed import PASSWORD, Seed, app_url

pytestmark = pytest.mark.infra

V = "/api/v1"
UP_TOOL = "get_forecast"
SOON = (utcnow() + dt.timedelta(days=30)).isoformat()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeUpstream:
    def __init__(self) -> None:
        self.description = "Get weather forecast"

    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": UP_TOOL,
                "description": self.description,
                "inputSchema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ]

    async def handle(self, request: Request) -> JSONResponse:
        body = await request.json()
        method = body.get("method")
        rid = body.get("id")
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"tools": self.tools()}})
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        )


@pytest.fixture
async def upstream() -> AsyncIterator[tuple[FakeUpstream, str]]:
    fake = FakeUpstream()
    app = Starlette(routes=[Route("/mcp", fake.handle, methods=["POST"])])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield fake, f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


@pytest.fixture
async def cp() -> httpx.AsyncClient:
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client


async def _login(cp: httpx.AsyncClient, seed: Seed, name: str = "admin") -> dict[str, str]:
    r = await cp.post(
        f"{V}/auth/login",
        json={"login_name": name, "password": PASSWORD, "totp": seed.code(name)},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


async def test_preview_hash_returns_stable_manifest(
    cp: httpx.AsyncClient, seeded: Seed, upstream: tuple[FakeUpstream, str]
) -> None:
    """对 FakeUpstream 拉 tools/list，返回与本地 tools_manifest_hash 一致的哈希。"""
    fake, base = upstream
    admin = await _login(cp, seeded)
    expected = tools_manifest_hash(fake.tools())

    conn_r = await cp.post(
        f"{V}/connections",
        headers=admin,
        json={
            "company_id": str(seeded.hq),
            "system_key": "preview_up_mcp",
            "env": "test",
            "base_url": base,
            "scheme": "http",
            "transport_exception_reason": "G1.2 preview local mock",
            "transport_exception_expires_at": SOON,
        },
    )
    assert conn_r.status_code == 201, conn_r.text
    cid = conn_r.json()["id"]
    sec_h = await _login(cp, seeded, "security")
    appr = await cp.post(f"{V}/connections/{cid}/approve-exception", headers=sec_h)
    assert appr.status_code == 200, appr.text

    preview = await cp.post(
        f"{V}/upstream-mcp-sources/preview-hash",
        headers=admin,
        json={"system_connection_id": cid, "mcp_path": "/mcp"},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["tools_manifest_hash"] == expected
    assert any(t.get("name") == UP_TOOL for t in body["tools"])
    preview2 = await cp.post(
        f"{V}/upstream-mcp-sources/preview-hash",
        headers=admin,
        json={"system_connection_id": cid, "mcp_path": "/mcp"},
    )
    assert preview2.json()["tools_manifest_hash"] == expected
    fake.description = "Changed"
    changed = await cp.post(
        f"{V}/upstream-mcp-sources/preview-hash",
        headers=admin,
        json={"system_connection_id": cid, "mcp_path": "/mcp"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["tools_manifest_hash"] != expected
    assert changed.json()["tools_manifest_hash"] == tools_manifest_hash(fake.tools())
    created = await cp.post(
        f"{V}/upstream-mcp-sources",
        headers=admin,
        json={
            "name": "preview_src",
            "system_connection_id": cid,
            "tools_manifest_hash": changed.json()["tools_manifest_hash"],
            "mcp_path": "/mcp",
            "pinned_tools": changed.json()["tools"],
        },
    )
    assert created.status_code == 201, created.text
