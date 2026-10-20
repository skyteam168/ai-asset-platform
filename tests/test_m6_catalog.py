"""M6b：套件组合约束、Catalog API、MCP 套件端点。

验收：未授权工具不出现在套件与目录；组合约束未经 G6 显式批准时拒绝发布。
"""

from __future__ import annotations

import pathlib
import uuid
from typing import Any

import httpx
import pytest

from aip.control_plane.approval import service as approval
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import Seed
from m2_fixtures import promote, tool_spec
from m3_world import World, connector, tool
from test_m4_gates import SECRETS, People, _people

pytestmark = pytest.mark.infra

READ_L3 = "production.mes.query_salary"
SEND_EXT = "ops.notify.send_email"
READ_L2 = "production.mes.query_work_orders"


def _bundle_spec(tools: list[str], name: str = "shift_pack") -> dict[str, Any]:
    return {
        "name": name,
        "capability_id": f"bundle.ops.{name}",
        "description": "班次相关工具套件，用于组合约束与目录过滤验收。",
        "tools": tools,
    }


async def _publish_member(
    db: Database, s: Seed, p: People, *, cap: str, classification: int, egress: str = "none", name: str | None = None
) -> str:
    """发布一个成员 Tool 到 active，返回 capability_id@1.0.0。"""
    spec = tool_spec(
        capability_id=cap,
        name=cap.split(".")[-1],
        data_classification=classification,
        egress_class=egress,
        risk_level="read",
    )
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="tool", name=name or cap, owner_org_id=s.hq, classification=classification
        )
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)
        await promote(conn, p.dev, vid)
    return f"{cap}@1.0.0"


async def test_bundle_composition_requires_g6_override(db: Database, seeded: Seed) -> None:
    """同一套件含 L3+ 读与 egress=external 时，G6 无显式批准不能通过。"""
    p = await _people(db, seeded)
    ref_l3 = await _publish_member(db, seeded, p, cap=READ_L3, classification=3)
    ref_ext = await _publish_member(db, seeded, p, cap=SEND_EXT, classification=2, egress="external")
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="bundle", name="bundle.ops.shift_pack", owner_org_id=seeded.hq, classification=3
        )
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", _bundle_spec([ref_l3, ref_ext]))
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["班次套件"]})
        g0 = await gates.submit(conn, p.dev, vid)
        assert g0["passed"] is True
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS)
        assert lab["passed"] is True
        g6 = uuid.UUID(lab["g6_approval"])
        g5 = uuid.UUID(lab["g5_approval"])
        req = await approval.request_row(conn, g6)
        assert req["payload"].get("composition_risk") is True

        with pytest.raises(PlatformError) as ei:
            await gates.decide(conn, p.sec, g6, approve=True, comment="")
        assert ei.value.code == ErrorCode.INVALID_ARGUMENT

        with pytest.raises(PlatformError):
            await gates.decide(conn, p.sec, g6, approve=True, comment="随便", results={"composition_override": False})

        # 业务验收先过；G6 显式批准后才能到 approved
        assert await gates.decide(conn, p.bo, g5, approve=True, comment="验收通过") == "approved"
        assert (
            await gates.decide(
                conn,
                p.sec,
                g6,
                approve=True,
                comment="业务需要",
                results={"composition_override": True, "reason": "班次通知需读工单密级数据，已评估外发范围"},
            )
            == "pending"
        )  # 组合风险强制双人
        assert (
            await gates.decide(
                conn,
                p.sec2,
                g6,
                approve=True,
                comment="复核同意",
                results={"composition_override": True, "reason": "班次通知需读工单密级数据，已评估外发范围"},
            )
            == "approved"
        )
        v = await registry.version_row(conn, vid)
        assert v["state"] == "approved"


async def test_catalog_and_bundle_hide_unauthorized(tmp_path: pathlib.Path) -> None:
    """未授权工具不出现在 GET /catalog 与 /mcp/bundles/{name} 的 tools/list。"""
    t_ok = tool(READ_L2, "mes", "q", data_classification=2)
    t_secret = tool(READ_L3, "mes", "q", data_classification=3)
    bundle = _bundle_spec([f"{READ_L2}@1.0.0", f"{READ_L3}@1.0.0"], name="shift_pack")
    w = World(
        tmp_path,
        [t_ok, t_secret],
        [connector("mes", "mock", {"operations": {"q": {"data": []}}})],
        bundles=[bundle],
        grant_caps={READ_L2},  # 只授权 L2
        static_token="aipk_catalog_test",
        exposure=["rest", "mcp"],
    )
    tok = "aipk_catalog_test"
    await w.ready()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://test") as c:
        r = await c.get("/api/v1/catalog", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        caps = {i["capability_id"] for i in r.json()["items"]}
        assert READ_L2 in caps
        assert READ_L3 not in caps
        assert "shift_pack" in r.json()["items"][0]["bundles"]

        mcp = await c.post(
            "/mcp/bundles/shift_pack",
            headers={"Authorization": f"Bearer {tok}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert mcp.status_code == 200, mcp.text
        names = {t["name"] for t in mcp.json()["result"]["tools"]}
        assert names == {READ_L2}

        # 全量 /mcp 同样看不到未授权工具
        all_mcp = await c.post(
            "/mcp",
            headers={"Authorization": f"Bearer {tok}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert {t["name"] for t in all_mcp.json()["result"]["tools"]} == {READ_L2}
