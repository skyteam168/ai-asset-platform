"""M5 演示数据（仅 dev）：一个“采购单审核”Workflow 与一个需审批的付款调用，供控制台“审批待办”体验。

用法：python tools/dev.py seed-demo（先 serve 起来）。可重复运行：首次建公司、消费方、Mock 连接器、
4 个 Tool 与 1 个 Workflow 并直接发布（演示用，跳过门禁）；之后每次只发起新的调用。

生成的待办都分派给平台管理员，用 bootstrap 的 admin 登录即可处理：
- 工作流任务：高金额采购单在 n3 等待“平台管理员审批”；低金额的一条会直接完成。
- 调用确认：付款给供应商（approval 类确认，审批人角色 platform_admin）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys
import time
from typing import Any

import httpx
from sqlalchemy import select

from aip.control_plane.conn import service as conn_service
from aip.control_plane.db import Database, DbScope
from aip.control_plane.grant import service as grants
from aip.control_plane.iam import consumers, orgs
from aip.control_plane.iam import tables as iam_t
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as rt

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE = ROOT / ".aip-data" / "demo-m5.json"
DP = "http://127.0.0.1:18082"
SYS = Actor.system_actor()
COMPANY_CODE, CLIENT_ID, CONNECTOR = "DEMO", "demo-agent", "demo_erp"
GET_PO, APPROVE, REVOKE, PAY = (
    "finance.erp.get_po",
    "finance.erp.approve_po",
    "finance.erp.revoke_approval",
    "finance.erp.pay_vendor",
)
WF = "agent.finance.po_review"
OPEN = {"type": "object"}


def tool(cap: str, op: str, description: str, *, write: bool = False, **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "capability_id": cap,
        "domain": cap.split(".")[0],
        "name": cap.split(".")[-1],
        "description": description,
        "input_schema": OPEN,
        "output_schema": OPEN,
        "connector_ref": {"connector": CONNECTOR, "operation": op},
        "risk_level": "write" if write else "read",
        "timeout": {"connect_ms": 1000, "read_ms": 3000, "deadline_ms": 5000},
        "data_classification": 2,
        "identity_mode": "service_account",
        "confirm_policy": "auto",
        "app_identity_allowed": True,
        "scope_binding": {},
    }
    if write:
        spec.update(
            idempotency={"idempotent": True},
            recovery="manual",
            reconcile={"operation": "query", "by": "idempotency_key"},
        )
    spec.update(over)
    return spec


TOOLS = [
    tool(GET_PO, "get_po", "按采购单号查询采购单的供应商、金额与明细，用于审核前取数。"),
    tool(APPROVE, "approve", "在 ERP 中批准指定采购单，批准后供应商可以发货。", write=True),
    tool(REVOKE, "revoke", "撤回已批准的采购单，作为批准操作的补偿。", write=True),
    tool(
        PAY,
        "pay",
        "向供应商付款，金额与收款方由调用方给出，执行前需要平台管理员审批。",
        write=True,
        confirm_policy="approval",
        approver_role="platform_admin",
        confirm_template="向 {{ args.vendor }} 付款 {{ args.amount }} 元（采购单 {{ args.po }}）",
    ),
]

WORKFLOW = {
    "name": "po_review",
    "capability_id": WF,
    "version": "1.0.0",
    "description": "采购单审核：取单 → 金额判断 → 高金额由平台管理员审批 → 在 ERP 中批准",
    "data_classification": 2,
    "input_schema": {
        "type": "object",
        "properties": {"po": {"type": "string"}, "amount": {"type": "number"}},
        "required": ["po", "amount"],
    },
    "budget": {"max_steps": 20, "max_llm_calls": 0, "timeout_s": 604800},
    "nodes": [
        {"id": "n1", "type": "tool", "ref": f"{GET_PO}@1.0.0", "args": {"po": "$input.po"}, "next": "n2"},
        {"id": "n2", "type": "condition", "expr": "input.amount > 10000", "then": "n3", "else": "n4"},
        {
            "id": "n3",
            "type": "human_task",
            "kind": "approve",
            "assignee": f"role:platform_admin@{COMPANY_CODE}",
            "summary": "高金额采购单需要平台管理员审批",
            "timeout_s": 86400,
            "next": "n4",
        },
        {
            "id": "n4",
            "type": "tool",
            "ref": f"{APPROVE}@1.0.0",
            "args": {"po": "$input.po"},
            "compensation": f"{REVOKE}@1.0.0",
            "compensation_args": {"po": "$input.po"},
            "next": "n5",
        },
        {"id": "n5", "type": "end", "output": {"po": "$input.po", "approved": "$n4.approved"}},
    ],
}

MOCK_OPS = {
    "get_po": {"response": {"po_no": "PO", "vendor": "越南钢材供应商", "lines": 3}},
    "approve": {"response": {"approved": True}, "write": True},
    "revoke": {"response": {"revoked": True}, "write": True},
    "pay": {"response": {"paid": True, "voucher": "PV-0001"}, "write": True},
    "query": {"response": {}},
}


async def _publish(conn: Any, type_: str, spec: dict[str, Any], owner: Any, company: Any) -> Any:
    name = spec["capability_id"]
    aid = (await conn.execute(select(rt.asset.c.id).where(rt.asset.c.name == name))).scalar()
    if aid is None:
        aid = await registry.create_asset(conn, SYS, type_=type_, name=name, owner_org_id=owner, classification=2)
        vid = await registry.create_version(conn, SYS, aid, "1.0.0", spec)
        for state in ("testing", "in_review", "approved", "canary", "active"):  # 演示：跳过门禁
            await registry.transition(conn, SYS, vid, state, "M5 演示数据")
        await registry.create_release(
            conn, SYS, vid, env="test", channel="stable", scope={"company_ids": [company]}, exposure=["rest", "mcp"]
        )
    return aid


async def setup() -> dict[str, Any]:
    if STATE.exists():
        return dict(json.loads(STATE.read_text(encoding="utf-8")))
    db = Database()
    try:
        async with db.tx(DbScope.system()) as conn:
            org_rows = (await conn.execute(select(iam_t.org_unit))).mappings().all()
            group = next(o for o in org_rows if o["type"] == "group")
            hq = next(o for o in org_rows if o["type"] == "company" and o["parent_id"] == group["id"])
            demo = next((o for o in org_rows if o["code"] == COMPANY_CODE), None)
            company = (
                demo["id"]
                if demo
                else (
                    await orgs.create_org(
                        conn,
                        SYS,
                        type_="company",
                        code=COMPANY_CODE,
                        name="演示公司（M5）",
                        parent_id=group["id"],
                        default_clearance=2,
                    )
                ).id
            )
            cid = await consumers.create_consumer(conn, SYS, client_id=CLIENT_ID, company_id=company, clearance_cap=3)
            _, token, _ = await consumers.issue_static_token(conn, SYS, cid)
            await conn_service.create_connector_instance(
                conn, SYS, name=CONNECTOR, kind="mock", connection_id=None, config={"operations": MOCK_OPS}
            )
            asset_ids = [await _publish(conn, "tool", t, hq["id"], company) for t in TOOLS]
            asset_ids.append(await _publish(conn, "workflow", WORKFLOW, hq["id"], company))
            ceiling = await grants.create_ceiling(
                conn, SYS, company_id=company, asset_ids=asset_ids, max_classification=3
            )
            expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=365)
            for aid in asset_ids:
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
    finally:
        await db.dispose()
    state = {"company_id": str(company), "consumer_id": str(cid), "token": token}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"已创建演示公司、消费方 {CLIENT_ID}、Mock 连接器、4 个 Tool 与 Workflow {WF}（令牌存于 {STATE.name}，仅 dev）"
    )
    return state


def invoke(c: httpx.Client, token: str, cap: str, args: dict[str, Any], key: str) -> dict[str, Any]:
    """数据面拉到新快照前会返回 NOT_FOUND / 权限不足：重试最多 60 秒。"""
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    end = time.monotonic() + 60
    while True:
        r = c.post(f"{DP}/api/v1/invoke/{cap}", json={"args": args}, headers=headers)
        body = dict(r.json())
        code = (body.get("error") or {}).get("code") or body.get("code")
        if code not in ("NOT_FOUND", "PERMISSION_DENIED", "UNAUTHENTICATED") or time.monotonic() > end:
            return body
        time.sleep(1)


def main() -> None:
    state = asyncio.run(setup())
    stamp = dt.datetime.now().strftime("%m%d%H%M%S")
    with httpx.Client(timeout=30) as c:
        try:
            c.get(f"{DP}/readyz").raise_for_status()
        except httpx.HTTPError:
            sys.exit("数据面没有运行：先执行 python tools/dev.py serve")
        high = invoke(c, state["token"], WF, {"po": f"PO-{stamp}-H", "amount": 38000}, f"demo-high-{stamp}")
        low = invoke(c, state["token"], WF, {"po": f"PO-{stamp}-L", "amount": 800}, f"demo-low-{stamp}")
        pay = invoke(
            c,
            state["token"],
            PAY,
            {"vendor": "越南钢材供应商", "amount": 125000, "po": f"PO-{stamp}-H"},
            f"demo-pay-{stamp}",
        )
        time.sleep(1.5)
        runs = {
            name: c.get(f"{DP}/api/v1/runs/{r['run_id']}", headers={"Authorization": f"Bearer {state['token']}"}).json()
            for name, r in (("高金额", high), ("低金额", low))
            if r.get("run_id")
        }
    for name, r in (("高金额采购单", high), ("低金额采购单", low), ("付款调用", pay)):
        print(f"{name}：status={r.get('status')} id={r.get('execution_id')} {r.get('error') or ''}")
    for name, v in runs.items():
        tasks = [t["summary"] for t in v.get("pending_tasks", [])]
        print(f"  Run（{name}）：{v.get('status')}，待办 {tasks or '无'}")
    if (pay.get("next_action") or {}).get("confirmation_id"):
        print(f"  付款确认：{pay['next_action']['message']}")
    print("用 admin 登录控制台 → 审批待办 → “工作流任务”与“调用确认”两页处理。")


if __name__ == "__main__":
    main()
