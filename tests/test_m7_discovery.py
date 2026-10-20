"""M7c：MCP 发现模式三元工具（architecture.md 10.3）。

- /mcp/discovery 的 tools/list 仅含 search/get/invoke_capability + get_run_status
- danger / 未授权搜不到；get/invoke 统一 Unknown
- 中文子串与越文去声调可命中
"""

from __future__ import annotations

import datetime as dt
import pathlib
import secrets
from typing import Any

import httpx
import jwt
import pytest

from aip.data_plane.mcp_gateway import (
    GET_CAPABILITY,
    GET_RUN_STATUS,
    INVOKE_CAPABILITY,
    SEARCH_CAPABILITIES,
)
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from m3_world import World, connector, tool

pytestmark = pytest.mark.infra

STATIC = "aipk_m7_discovery"
CONSUMER_KEY = SigningKey.generate()
READ_OK = "production.mes.query_work_orders"
READ_SECRET = "finance.erp.salary_summary"  # L4，用户 L2 无权
DANGER = "ops.mes.force_shutdown"
VI_CAP = "hr.leave.submit_request"


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
) -> dict[str, Any]:
    p = dict(params or {})
    if version:
        p["_meta"] = {"io.modelcontextprotocol/protocolVersion": version}
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": p}


@pytest.fixture
async def world(tmp_path: pathlib.Path) -> World:
    tools = [
        tool(
            READ_OK,
            "mes",
            "q",
            description="查询本厂工单列表，支持按状态筛选。",
            data_classification=2,
        ),
        tool(READ_SECRET, "erp", "q", description="薪资汇总", data_classification=4),
        tool(
            DANGER,
            "mes",
            "kill",
            description="强制关停产线",
            risk_level="danger",
            data_classification=2,
        ),
        tool(
            VI_CAP,
            "hr",
            "leave",
            description="Gửi đơn xin nghỉ phép cho nhân viên",
            data_classification=2,
            input_schema={"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"]},
            output_schema={"type": "object"},
            scope_binding={},
        ),
    ]
    w = World(
        tmp_path,
        tools,
        [
            connector(
                "mes",
                "mock",
                {
                    "operations": {
                        "q": {"response": [{"wo_no": "WO-1"}]},
                        "kill": {"response": {"ok": True}, "write": True},
                    }
                },
            ),
            connector("erp", "mock", {"operations": {"q": {"response": {}}}}),
            connector("hr", "mock", {"operations": {"leave": {"response": {"ok": True}}}}),
        ],
        static_token=STATIC,
        consumer_jwk=CONSUMER_KEY.public_jwk(),
        grant_caps={READ_OK, DANGER, VI_CAP},  # 明确不授 READ_SECRET；DANGER 有授权仍不进发现
        exposure=["rest", "mcp"],
    )
    await w.ready()
    return w


async def test_discovery_tools_list_only_meta(world: World) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app), base_url="http://t") as c:
        r = await c.post("/mcp/discovery", headers=headers(), json=rpc("tools/list"))
        names = [t["name"] for t in r.json()["result"]["tools"]]
        assert names == sorted([GET_CAPABILITY, GET_RUN_STATUS, INVOKE_CAPABILITY, SEARCH_CAPABILITIES])

        # 直连端点不含元工具
        all_mcp = await c.post("/mcp", headers=headers(), json=rpc("tools/list", rid=2))
        direct = {t["name"] for t in all_mcp.json()["result"]["tools"]}
        assert SEARCH_CAPABILITIES not in direct
        assert READ_OK in direct


async def test_search_hides_unauthorized_and_danger(world: World) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app), base_url="http://t") as c:
        r = await c.post(
            "/mcp/discovery",
            headers=headers(),
            json=rpc(
                "tools/call",
                {"name": SEARCH_CAPABILITIES, "arguments": {"query": "工单"}},
            ),
        )
        items = r.json()["result"]["structuredContent"]["items"]
        ids = {i["capability_id"] for i in items}
        assert READ_OK in ids
        assert READ_SECRET not in ids
        assert DANGER not in ids

        # danger 关键词也不出现
        dang = await c.post(
            "/mcp/discovery",
            headers=headers(),
            json=rpc(
                "tools/call",
                {"name": SEARCH_CAPABILITIES, "arguments": {"query": "关停"}},
                rid=2,
            ),
        )
        assert dang.json()["result"]["structuredContent"]["items"] == []


async def test_vietnamese_accent_fold_search(world: World) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app), base_url="http://t") as c:
        # 查询无声调：nghi phep → 命中 nghỉ phép
        r = await c.post(
            "/mcp/discovery",
            headers=headers(),
            json=rpc(
                "tools/call",
                {"name": SEARCH_CAPABILITIES, "arguments": {"query": "nghi phep"}},
            ),
        )
        ids = {i["capability_id"] for i in r.json()["result"]["structuredContent"]["items"]}
        assert VI_CAP in ids


async def test_get_and_invoke_capability(world: World) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app), base_url="http://t") as c:
        got = await c.post(
            "/mcp/discovery",
            headers=headers(),
            json=rpc(
                "tools/call",
                {"name": GET_CAPABILITY, "arguments": {"capability_id": READ_OK}},
            ),
        )
        detail = got.json()["result"]["structuredContent"]
        assert detail["name"] == READ_OK
        assert detail["inputSchema"]["required"] == ["factory_id"]

        # 未授权 / danger / 不存在 → 同一错误形态
        for cap in (READ_SECRET, DANGER, "no.such.cap"):
            bad = await c.post(
                "/mcp/discovery",
                headers=headers(),
                json=rpc(
                    "tools/call",
                    {"name": GET_CAPABILITY, "arguments": {"capability_id": cap}},
                    rid=2,
                ),
            )
            assert bad.json()["error"]["message"] == f"Unknown capability: {cap}"

        inv = await c.post(
            "/mcp/discovery",
            headers=headers(),
            json=rpc(
                "tools/call",
                {
                    "name": INVOKE_CAPABILITY,
                    "arguments": {"capability_id": READ_OK, "arguments": {"factory_id": "F001"}},
                },
                rid=3,
            ),
        )
        assert inv.json()["result"]["isError"] is False
        assert inv.json()["result"]["structuredContent"] == {"items": [{"wo_no": "WO-1"}]}

        deny = await c.post(
            "/mcp/discovery",
            headers=headers(),
            json=rpc(
                "tools/call",
                {
                    "name": INVOKE_CAPABILITY,
                    "arguments": {"capability_id": DANGER, "arguments": {}},
                },
                rid=4,
            ),
        )
        assert deny.json()["error"]["message"] == f"Unknown capability: {DANGER}"
