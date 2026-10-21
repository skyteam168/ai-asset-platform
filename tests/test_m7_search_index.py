"""M7m：发现检索物化 search_text 与 CJK 双字（architecture.md 10.3）。"""

from __future__ import annotations

import datetime as dt
import pathlib
import secrets
from typing import Any

import httpx
import jwt
import pytest

from aip.data_plane.mcp_gateway import SEARCH_CAPABILITIES
from aip.libs.common.config import get_settings
from aip.libs.searchtext import build_search_text, fold, query_tokens, score_release
from aip.libs.tokens.keys import SigningKey
from m3_world import World, connector, tool

pytestmark = pytest.mark.infra

STATIC = "aipk_m7m_search"
CONSUMER_KEY = SigningKey.generate()
READ_OK = "production.mes.query_work_orders"
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


def rpc(args: dict[str, Any], *, rid: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {
            "name": SEARCH_CAPABILITIES,
            "arguments": args,
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
        },
    }


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
            connector("hr", "mock", {"operations": {"leave": {"response": {"ok": True}}}}),
        ],
        static_token=STATIC,
        consumer_jwk=CONSUMER_KEY.public_jwk(),
        grant_caps={READ_OK, DANGER, VI_CAP},
        exposure=["rest", "mcp"],
    )
    await w.ready()
    return w


def test_query_tokens_cjk_bigrams() -> None:
    assert query_tokens("查询工单") == ["查询", "工单"]
    assert query_tokens("工单") == ["工单"]
    assert "nghi" in query_tokens("nghi phep")
    assert "phep" in query_tokens("nghi phep")


def test_build_search_text_folded_and_materialized() -> None:
    rel = {
        "capability_id": "production.mes.query_work_orders",
        "name": "query_work_orders",
        "spec": {
            "capability_id": "production.mes.query_work_orders",
            "description": "Gửi đơn xin nghỉ",
            "domain": "production",
        },
    }
    text = build_search_text(rel)
    assert text == fold(text)
    assert "nghi" in text  # 去调后
    assert "gui" in text  # gửi → gui
    rel["search_text"] = text
    assert score_release(rel, "nghi") > 0


def test_score_legacy_snapshot_without_search_text() -> None:
    rel = {
        "capability_id": "a.b.c",
        "spec": {"description": "查询本厂工单列表", "name": "c"},
    }
    assert score_release(rel, "查询工单") > 0
    assert score_release(rel, "工单") > 0


async def test_chinese_phrase_and_vietnamese_discovery(world: World) -> None:
    # 快照已物化 search_text
    releases = world.app.state.releases.data["releases"]
    mes = next(r for r in releases if r["capability_id"] == READ_OK)
    assert mes.get("search_text")
    assert "工单" in mes["search_text"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app), base_url="http://t") as c:
        for q in ("工单", "查询工单"):
            r = await c.post("/mcp/discovery", headers=headers(), json=rpc({"query": q}))
            ids = {i["capability_id"] for i in r.json()["result"]["structuredContent"]["items"]}
            assert READ_OK in ids, q
            assert DANGER not in ids

        vi = await c.post(
            "/mcp/discovery",
            headers=headers(),
            json=rpc({"query": "nghi phep"}, rid=2),
        )
        assert VI_CAP in {i["capability_id"] for i in vi.json()["result"]["structuredContent"]["items"]}

        dang = await c.post(
            "/mcp/discovery",
            headers=headers(),
            json=rpc({"query": "关停"}, rid=3),
        )
        assert dang.json()["result"]["structuredContent"]["items"] == []
