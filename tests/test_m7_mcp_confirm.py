"""M7b：MCP MRTR / elicitation 确认通道（architecture.md 4.5 通道 1）。

- 客户端声明 elicitation 时 user_confirm → resultType=input_required
- 重试原 tools/call + _meta 确认决策 → via=mcp 执行 / 拒绝
- 未声明能力、approval 类仍走错误 + next_action（回调 / 平台待办）
"""

from __future__ import annotations

import datetime as dt
import pathlib
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aip.connectors.mock import MockConnector
from aip.control_plane.db import Database
from aip.data_plane.mcp_gateway import ELICITATION_EXT, NS
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from iam_seed import app_url
from m3_world import World, connector, write_tool
from test_m5_workflow import OPEN

pytestmark = pytest.mark.infra

STATIC = "aipk_m7_confirm_static"
CONSUMER_KEY = SigningKey.generate()
PAY = "finance.erp.pay_vendor"
WIRE = "finance.erp.wire_transfer"
ARGS = {"vendor": "ACME", "amount": 800}
ELICIT_META = {ELICITATION_EXT: {}}


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
    return {"Authorization": f"Bearer {STATIC}", "X-User-Assertion": assertion()}


def rpc(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    version: str | None = "2026-07-28",
    rid: int = 1,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    p = dict(params or {})
    meta: dict[str, Any] = {}
    if version:
        meta["io.modelcontextprotocol/protocolVersion"] = version
    if extra_meta:
        meta.update(extra_meta)
    if meta:
        p["_meta"] = meta
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": p}


@pytest.fixture
async def world(db: Database, tmp_path: pathlib.Path) -> AsyncIterator[tuple[World, AsyncEngine]]:
    engine = create_async_engine(app_url())
    MockConnector.executed.clear()
    tools = [
        write_tool(
            PAY,
            "erp",
            "pay",
            input_schema=OPEN,
            scope_binding={},
            confirm_policy="user_confirm",
            confirm_template="向 {{ args.vendor }} 付款 {{ args.amount }} 元",
        ),
        write_tool(
            WIRE,
            "erp",
            "pay",
            input_schema=OPEN,
            scope_binding={},
            confirm_policy="approval",
            approver_role="data_owner",
        ),
    ]
    w = World(
        tmp_path,
        tools,
        [connector("erp", "mock", {"operations": {"pay": {"response": {"paid": True}, "write": True}}})],
        runtime_engine=engine,
        static_token=STATIC,
        consumer_jwk=CONSUMER_KEY.public_jwk(),
    )
    await w.ready()
    yield w, engine
    await engine.dispose()


async def call_pay(
    c: httpx.AsyncClient,
    *,
    args: dict[str, Any] | None = None,
    extra_meta: dict[str, Any] | None = None,
    rid: int = 1,
    key: str | None = "pay-mcp-1",
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": PAY, "arguments": args or ARGS}
    meta = dict(extra_meta or {})
    if key:
        meta[f"{NS}/idempotencyKey"] = key
    r = await c.post(
        "/mcp",
        headers=headers(),
        json=rpc("tools/call", params, rid=rid, extra_meta=meta or None),
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_capabilities_advertise_elicitation(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        init = await c.post(
            "/mcp",
            headers=headers(),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        )
        caps = init.json()["result"]["capabilities"]
        assert "elicitation" in caps
        disc = await c.post("/mcp", headers=headers(), json=rpc("server/discover"))
        assert "elicitation" in disc.json()["result"]["capabilities"]


async def test_elicitation_input_required_then_approve(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        first = await call_pay(c, extra_meta=ELICIT_META)
        body = first["result"]
        assert body["resultType"] == "input_required"
        assert body["isError"] is False
        assert body["structuredContent"]["summary"] == "向 ACME 付款 800 元"
        cid = body["structuredContent"]["confirmation_id"]
        assert body["_meta"][f"{NS}/confirmation_id"] == cid
        assert body["inputSchema"]["required"] == ["decision"]
        assert MockConnector.executed == {}

        second = await call_pay(
            c,
            rid=2,
            key="pay-mcp-1",
            extra_meta={
                **ELICIT_META,
                f"{NS}/confirmationId": cid,
                f"{NS}/confirmationDecision": "approve",
            },
        )
        ok = second["result"]
        assert ok["isError"] is False
        assert ok["structuredContent"] == {"paid": True}
        assert list(MockConnector.executed) == [f"erp:{cid}"]

        # 同一确认再 approve：幂等，不二次执行
        again = await call_pay(
            c,
            rid=3,
            key="pay-mcp-1b",
            extra_meta={
                **ELICIT_META,
                f"{NS}/confirmationId": cid,
                f"{NS}/confirmationDecision": "approve",
            },
        )
        assert again["result"]["isError"] is False
        assert list(MockConnector.executed) == [f"erp:{cid}"]


async def test_elicitation_reject_then_cannot_approve(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        first = await call_pay(c, key="pay-rej", extra_meta=ELICIT_META)
        cid = first["result"]["structuredContent"]["confirmation_id"]
        rej = await call_pay(
            c,
            rid=2,
            key="pay-rej",
            extra_meta={
                **ELICIT_META,
                f"{NS}/confirmationId": cid,
                f"{NS}/confirmationDecision": "reject",
                f"{NS}/confirmationComment": "取消",
            },
        )
        assert rej["result"]["isError"] is True
        assert MockConnector.executed == {}
        late = await call_pay(
            c,
            rid=3,
            key="pay-rej2",
            extra_meta={
                **ELICIT_META,
                f"{NS}/confirmationId": cid,
                f"{NS}/confirmationDecision": "approve",
            },
        )
        assert "error" in late
        assert "PRECONDITION_FAILED" in late["error"]["message"]


async def test_elicitation_tampered_args_rejected(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        first = await call_pay(c, key="pay-tamper", extra_meta=ELICIT_META)
        cid = first["result"]["structuredContent"]["confirmation_id"]
        bad = await call_pay(
            c,
            rid=2,
            key="pay-tamper",
            args={**ARGS, "amount": 1},
            extra_meta={
                **ELICIT_META,
                f"{NS}/confirmationId": cid,
                f"{NS}/confirmationDecision": "approve",
            },
        )
        assert "error" in bad
        assert "CONFIRMATION_MISMATCH" in bad["error"]["message"]
        assert MockConnector.executed == {}


async def test_without_elicitation_returns_error_next_action(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        r = await call_pay(c, key="pay-no-elicit")
        body = r["result"]
        assert body["isError"] is True
        assert body.get("resultType") == "complete"
        nxt = body["_meta"][f"{NS}/next_action"]
        assert nxt["type"] == "confirm" and nxt["confirmation_id"]
        assert nxt["message"] == "向 ACME 付款 800 元"


async def test_approval_never_input_required(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        params = {"name": WIRE, "arguments": ARGS}
        r = await c.post(
            "/mcp",
            headers=headers(),
            json=rpc(
                "tools/call",
                params,
                extra_meta={**ELICIT_META, f"{NS}/idempotencyKey": "wire-1"},
            ),
        )
        body = r.json()["result"]
        assert body["isError"] is True
        assert body.get("resultType") != "input_required"
        assert body["_meta"][f"{NS}/next_action"]["type"] == "confirm"
