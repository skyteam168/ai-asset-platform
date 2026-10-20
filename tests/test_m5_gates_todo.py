"""M5：G3 稳定性门禁、Workflow 的 G0 / 组合门禁、平台待办的控制面授权。"""

from __future__ import annotations

import copy
import itertools
import uuid
from typing import Any

import httpx
import pytest

from aip.connectors.base import ConnectorResult
from aip.connectors.mock import MockConnector
from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.control_plane.todo_api import DataPlane
from iam_seed import Seed, app_url
from m2_fixtures import promote
from test_m1_api import login
from test_m4_gates import CASES, SECRETS, People, _draft, _people, _state

pytestmark = pytest.mark.infra
IDS = {
    "c-approval": "0192e3a0-0000-7000-8000-00000000c0a1",
    "c-own": "0192e3a0-0000-7000-8000-00000000c0a2",
    "c-user": "0192e3a0-0000-7000-8000-00000000c0a3",
    "t-vn": "0192e3a0-0000-7000-8000-00000000d0a1",
    "t-kh": "0192e3a0-0000-7000-8000-00000000d0a2",
}


async def test_g3_rejects_nondeterministic_tool(db: Database, seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    p = await _people(db, seeded)
    vid = await _draft(db, seeded, p)
    counter = itertools.count()
    real = MockConnector.execute

    async def flaky(self: MockConnector, op: str, params: dict[str, Any], ctx: Any) -> ConnectorResult:
        res = await real(self, op, params, ctx)
        if op == "q" and isinstance(res.data, list):  # 同样的输入，偶尔返回不同的数量
            data = copy.deepcopy(res.data)
            data[0]["qty"] += next(counter) % 3 == 0
            return ConnectorResult(data=data)
        return res

    monkeypatch.setattr(MockConnector, "execute", flaky)
    async with db.tx(DbScope.system()) as conn:
        assert (await gates.submit(conn, p.dev, vid))["passed"]
    async with db.tx(DbScope.system()) as conn:
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS)
    assert lab["passed"] is False
    assert all(c["passed"] for c in lab["cases"])  # G1 / G2 单次都通过
    assert lab["stability"]["本厂查询"]["runs"] == 20 and lab["stability"]["本厂查询"]["distinct"] > 1
    assert lab["stability"]["他厂被拒"]["distinct"] == 1
    assert await _state(db, vid) == "draft"
    async with db.tx(DbScope.system()) as conn:
        history = [(g["gate"], g["status"]) for g in await gates.gate_history(conn, vid)]
    assert ("G3", "failed") in history


def _workflow(tool_ref: str, **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "work_order_digest",
        "capability_id": "agent.production.work_order_digest",
        "version": "1.0.0",
        "description": "汇总本厂在制工单",
        "input_schema": {"type": "object", "properties": {"factory_id": {"type": "string"}}},
        "budget": {"max_steps": 10, "max_llm_calls": 0, "timeout_s": 300},
        "nodes": [
            {"id": "q", "type": "tool", "ref": tool_ref, "args": {"factory_id": "$input.factory_id"}, "next": "done"},
            {"id": "done", "type": "end", "output": {"rows": "$q"}},
        ],
    }
    spec.update(over)
    return spec


async def _workflow_version(db: Database, s: Seed, p: People, spec: dict[str, Any], name: str) -> uuid.UUID:
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(conn, p.dev, type_="workflow", name=name, owner_org_id=s.hq, classification=2)
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["主管查看工单汇总"]})
    return vid


async def test_workflow_g0_and_composition_gates(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    tool_vid = await _draft(db, seeded, p, cases=CASES)
    cap = "production.mes.query_work_orders"
    ref = f"{cap}@1.0.0"

    bad = _workflow(ref)
    bad["nodes"][0]["next"] = "q"  # 环
    wid_bad = await _workflow_version(db, seeded, p, bad, "wf.bad")
    unknown = await _workflow_version(db, seeded, p, _workflow(f"{cap}@9.9.9"), "wf.unknown")
    async with db.tx(DbScope.system()) as conn:
        g0 = await gates.submit(conn, p.dev, wid_bad)
        assert g0["passed"] is False and any("环" in x for x in g0["problems"])
        g0 = await gates.submit(conn, p.dev, unknown)
        assert g0["passed"] is False and any("9.9.9" in x for x in g0["problems"])

    # Tool 仍在 draft：Workflow 能通过 G0（引用可解析），但组合门禁不通过
    wid = await _workflow_version(db, seeded, p, _workflow(ref), "wf.digest")
    async with db.tx(DbScope.system()) as conn:
        assert (await gates.submit(conn, p.dev, wid))["passed"]
        lab = await gates.run_lab(conn, p.dev, wid, secrets=SECRETS)
    assert lab["passed"] is False and lab["tool_versions"] == {ref: "draft"}
    assert await _state(db, wid) == "draft"

    async with db.tx(DbScope.system()) as conn:
        await promote(conn, p.admin, tool_vid, to="approved")
        assert (await gates.submit(conn, p.dev, wid))["passed"]
        lab = await gates.run_lab(conn, p.dev, wid, secrets=SECRETS)
    assert lab["passed"] and lab["mode"] == "composition" and lab["g6_approval"]
    assert await _state(db, wid) == "in_review"


# ---------------------------------------------------------------- 平台待办（控制面授权，数据面用假实现）


class FakeDataPlane:
    def __init__(self, s: Seed) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        vn, worker = str(s.vn), str(s.users["vn_worker"])
        self.confirmations = {
            IDS["c-approval"]: {
                "id": IDS["c-approval"],
                "kind": "approval",
                "company_id": vn,
                "approver_role": "data_owner",
                "end_user_id": worker,
                "summary": "大额付款",
                "status": "pending",
            },
            IDS["c-own"]: {
                "id": IDS["c-own"],
                "kind": "approval",
                "company_id": vn,
                "approver_role": "data_owner",
                "end_user_id": str(s.users["security"]),
                "summary": "安全岗自己发起的付款",
                "status": "pending",
            },
            IDS["c-user"]: {
                "id": IDS["c-user"],
                "kind": "user_confirm",
                "company_id": vn,
                "approver_role": None,
                "end_user_id": worker,
                "summary": "员工本人确认",
                "status": "pending",
            },
        }
        self.tasks = {
            IDS["t-vn"]: {
                "id": IDS["t-vn"],
                "run_id": "r1",
                "kind": "approve",
                "company_id": vn,
                "assignee_user_id": None,
                "assignee_role": "company_admin",
                "assignee_org_id": vn,
                "summary": "越南厂审批",
            },
            IDS["t-kh"]: {
                "id": IDS["t-kh"],
                "run_id": "r2",
                "kind": "approve",
                "company_id": str(s.kh),
                "assignee_user_id": None,
                "assignee_role": "company_admin",
                "assignee_org_id": str(s.kh),
                "summary": "柬埔寨厂审批",
            },
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        path = request.url.path.removeprefix("/api/v1/internal")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        assert request.headers["Authorization"].startswith("Bearer ")
        if path == "/todo":
            return httpx.Response(
                200, json={"confirmations": list(self.confirmations.values()), "tasks": list(self.tasks.values())}
            )
        kind, ident, *rest = path.strip("/").split("/")
        item = (self.confirmations if kind == "confirmations" else self.tasks).get(ident)
        if item is None:
            return httpx.Response(404, json={"code": "NOT_FOUND", "detail": "不存在"})
        if rest == ["decide"]:
            return httpx.Response(200, json={"status": "succeeded", "id": ident})
        return httpx.Response(200, json=item)


@pytest.fixture
async def todo_env(seeded: Seed) -> tuple[httpx.AsyncClient, FakeDataPlane]:
    fake = FakeDataPlane(seeded)
    app = create_cp(db=Database(app_url()))
    app.state.data_plane = DataPlane("http://dp", "svc", transport=httpx.MockTransport(fake.handler))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp"), fake


async def test_todo_authorization(todo_env: tuple[httpx.AsyncClient, FakeDataPlane], seeded: Seed) -> None:
    cp, fake = todo_env
    sec = await login(cp, seeded, "security")
    vn = await login(cp, seeded, "vn_admin")
    kh = await login(cp, seeded, "kh_admin")

    mine = (await cp.get("/api/v1/todo", headers=sec)).json()
    # 数据 Owner：看得到别人发起的审批，看不到自己发起的（职责分离），也看不到员工本人确认
    assert [c["id"] for c in mine["confirmations"]] == [IDS["c-approval"]] and mine["tasks"] == []
    assert [t["id"] for t in (await cp.get("/api/v1/todo", headers=vn)).json()["tasks"]] == [IDS["t-vn"]]
    assert [t["id"] for t in (await cp.get("/api/v1/todo", headers=kh)).json()["tasks"]] == [IDS["t-kh"]]

    ok = await cp.post(
        f"/api/v1/todo/confirmations/{IDS['c-approval']}/decide", json={"decision": "approve"}, headers=sec
    )
    assert ok.status_code == 200 and ok.json()["status"] == "succeeded"
    expected = {"decision": "approve", "user_id": str(seeded.users["security"]), "comment": None}
    assert fake.calls[-1] == ("POST", f"/confirmations/{IDS['c-approval']}/decide", expected)
    own = await cp.post(f"/api/v1/todo/confirmations/{IDS['c-own']}/decide", json={"decision": "approve"}, headers=sec)
    assert own.status_code == 403
    assert (
        await cp.post(f"/api/v1/todo/confirmations/{IDS['c-user']}/decide", json={"decision": "approve"}, headers=sec)
    ).status_code == 403
    assert (
        await cp.post(
            f"/api/v1/todo/confirmations/{IDS['c-approval']}/decide", json={"decision": "approve"}, headers=vn
        )
    ).status_code == 403

    denied = await cp.post(f"/api/v1/todo/tasks/{IDS['t-vn']}/decide", json={"decision": "approve"}, headers=kh)
    assert denied.status_code == 403
    done = await cp.post(
        f"/api/v1/todo/tasks/{IDS['t-vn']}/decide", json={"decision": "reject", "comment": "数量不对"}, headers=vn
    )
    assert done.status_code == 200
    assert fake.calls[-1][2]["user_id"] == str(seeded.users["vn_admin"])
    missing = await cp.post(f"/api/v1/todo/tasks/{uuid.uuid4()}/decide", json={"decision": "approve"}, headers=vn)
    assert missing.status_code == 404 and missing.json()["code"] == "NOT_FOUND"
