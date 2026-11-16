"""为企业自建 Agent 工作平台联调：发布一条同步可完成的测试 Workflow，并签发消费方令牌。

能力：agent.ops.platform_ping（无人工节点、无下游依赖）
暴露：rest + mcp + a2a
用法：先 serve，再：
  .venv/Scripts/python tools/seed_agent_platform_ping.py

凭据写入 .aip-data/agent-platform-test.json（仅本地，勿提交）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select

from aip.control_plane.db import Database, DbScope
from aip.control_plane.grant import service as grants
from aip.control_plane.iam import consumers, orgs
from aip.control_plane.iam import tables as iam_t
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as rt

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE = ROOT / ".aip-data" / "agent-platform-test.json"
DP = "http://127.0.0.1:18082"
CP = "http://127.0.0.1:18081"
SYS = Actor.system_actor()
CLIENT_ID = "agent-platform-test"
WF = "agent.ops.platform_ping"

WORKFLOW = {
    "name": "platform_ping",
    "capability_id": WF,
    "version": "1.0.0",
    "description": "联调用垂直 Agent：回显 message，并标记来源为 AIP Workflow（无人工、无下游）。",
    "data_classification": 1,
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "任意短文本，将原样回显"},
            "from_agent": {"type": "string", "description": "调用方 Agent 名（可选）"},
        },
        "required": ["message"],
    },
    "budget": {"max_steps": 5, "max_llm_calls": 0, "timeout_s": 60},
    "nodes": [
        {
            "id": "n1",
            "type": "end",
            "output": {
                "ok": True,
                "echo": "$input.message",
                "from_agent": "$input.from_agent",
                "capability_id": WF,
                "hint": "AIP workflow agent.ops.platform_ping",
            },
        }
    ],
}


async def _publish_workflow(conn: Any, owner: Any, company: Any) -> Any:
    aid = (await conn.execute(select(rt.asset.c.id).where(rt.asset.c.name == WF))).scalar()
    if aid is not None:
        return aid
    aid = await registry.create_asset(
        conn, SYS, type_="workflow", name=WF, owner_org_id=owner, classification=1
    )
    vid = await registry.create_version(conn, SYS, aid, "1.0.0", WORKFLOW)
    for state in ("testing", "in_review", "approved", "canary", "active"):
        await registry.transition(conn, SYS, vid, state, "agent-platform 联调")
    await registry.create_release(
        conn,
        SYS,
        vid,
        env="test",
        channel="stable",
        scope={"company_ids": [company]},
        exposure=["rest", "mcp", "a2a"],
    )
    return aid


async def setup() -> dict[str, Any]:
    db = Database()
    try:
        async with db.tx(DbScope.system()) as conn:
            org_rows = (await conn.execute(select(iam_t.org_unit))).mappings().all()
            group = next(o for o in org_rows if o["type"] == "group")
            hq = next(o for o in org_rows if o["type"] == "company" and o["parent_id"] == group["id"])
            company = hq["id"]
            existing = (
                await conn.execute(select(iam_t.consumer.c.id).where(iam_t.consumer.c.client_id == CLIENT_ID))
            ).scalar()
            if existing:
                cid = existing
                # 已有消费方：轮换静态令牌便于拿到明文
                _, token, _ = await consumers.issue_static_token(conn, SYS, cid)
            else:
                cid = await consumers.create_consumer(
                    conn, SYS, client_id=CLIENT_ID, company_id=company, clearance_cap=3
                )
                _, token, _ = await consumers.issue_static_token(conn, SYS, cid)
            aid = await _publish_workflow(conn, hq["id"], company)
            ceiling = await grants.create_ceiling(
                conn, SYS, company_id=company, asset_ids=[aid], max_classification=3
            )
            expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=365)
            # 幂等：重复 grant 可能冲突，吞掉已存在
            try:
                await grants.create_grant(
                    conn,
                    SYS,
                    asset_id=aid,
                    grantee_type="consumer",
                    grantee_id=cid,
                    env="test",
                    expires_at=expires,
                    ceiling_id=ceiling,
                )
            except Exception as e:
                if "already" not in str(e).lower() and "冲突" not in str(e) and "duplicate" not in str(e).lower():
                    # Grant 已存在时多数抛 PlatformError；继续用旧 grant
                    from aip.libs.common.errors import PlatformError

                    if not isinstance(e, PlatformError):
                        raise
    finally:
        await db.dispose()

    doc = {
        "capability_id": WF,
        "client_id": CLIENT_ID,
        "consumer_id": str(cid),
        "company_id": str(company),
        "token": token,
        "mcp_url": f"{DP}/mcp",
        "rest_invoke": f"{DP}/api/v1/invoke/{WF}",
        "run_status": f"{DP}/api/v1/runs/{{run_id}}",
        "a2a_card": f"{DP}/.well-known/agent-card.json",
        "a2a_rpc": f"{DP}/a2a",
        "console": f"{CP}/console/",
        "sample_args": {"message": "hello from my agent platform", "from_agent": "internal-agent"},
        "notes": [
            "本地数据面默认 HTTP；生产须 HTTPS。",
            "MCP 请用 Authorization: Bearer <token>，不要复用同一 X-User-Assertion。",
            "tools/list 应见 agent.ops.platform_ping 与 aip.get_run_status。",
            "本 Workflow 同步完成，一般无 run 等待；复杂流程用 aip.get_run_status。",
        ],
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


def wait_ready(c: httpx.Client) -> None:
    end = time.monotonic() + 30
    while time.monotonic() < end:
        try:
            if c.get(f"{DP}/readyz").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    sys.exit("数据面未就绪：先执行 python tools/dev.py serve")


def smoke(doc: dict[str, Any]) -> None:
    token = doc["token"]
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=30) as c:
        wait_ready(c)
        # 等快照拉取
        body: dict[str, Any] = {}
        for _ in range(45):
            r = c.post(
                doc["rest_invoke"],
                headers={**headers, "Idempotency-Key": f"ping-{uuid.uuid4().hex[:12]}"},
                json={"args": doc["sample_args"]},
            )
            body = dict(r.json())
            code = (body.get("error") or {}).get("code") or body.get("code")
            if code not in ("NOT_FOUND", "PERMISSION_DENIED", "UNAUTHENTICATED"):
                break
            time.sleep(1)
        print("REST invoke:", json.dumps(body, ensure_ascii=False)[:500])
        mcp = c.post(
            doc["mcp_url"],
            headers={
                **headers,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        names: list[str] = []
        try:
            tools = (mcp.json().get("result") or {}).get("tools") or []
            names = [t.get("name") for t in tools if isinstance(t, dict)]
        except Exception:
            print("MCP tools/list raw:", mcp.text[:400])
        print("MCP tools 含 ping:", WF in names, "| 工具数:", len(names))
        if WF in names:
            call = c.post(
                doc["mcp_url"],
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": WF,
                        "arguments": {
                            "message": "mcp call ok",
                            "from_agent": "seed-script",
                        },
                    },
                },
            )
            print("MCP tools/call:", call.text[:600])


def main() -> None:
    doc = asyncio.run(setup())
    print(f"已写入 {STATE}")
    print(f"capability_id = {WF}")
    print(f"MCP URL      = {doc['mcp_url']}")
    print(f"token        = {doc['token'][:16]}…（全文见 json）")
    smoke(doc)
    print("\n在你的 Agent 平台配置：")
    print(f"  Transport: Streamable HTTP / MCP")
    print(f"  URL:       {doc['mcp_url']}")
    print(f"  Header:    Authorization: Bearer <token>")
    print(f"  调用工具:  {WF}")
    print(f"  参数:      {json.dumps(doc['sample_args'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
