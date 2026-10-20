"""M5：写操作确认流（architecture.md 4.5）。

验收：确认参数被篡改时拒绝执行；同一 confirmation 只执行一次。
另覆盖：确认文案来自 Tool Spec 模板；回调确认必须是同一用户的新断言（防重放、iat 不早于创建、换人无效）；
approval 类不能经回调确认；平台待办（服务凭据）审批后执行；拒绝与过期。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aip.connectors.mock import MockConnector
from aip.control_plane.db import Database
from aip.libs.common.config import get_settings
from iam_seed import app_url
from m3_world import KH, VN, World, connector, write_tool
from test_m5_workflow import CONSUMER_KEY, OPEN, STATIC, assertion

pytestmark = pytest.mark.infra

PAY = "finance.erp.pay_vendor"
WIRE = "finance.erp.wire_transfer"
OTHER_USER = "0192e3a0-0000-7000-8000-0000000000a2"
ARGS = {"vendor": "ACME", "amount": 800}


@pytest.fixture
async def world(db: Database, tmp_path: pathlib.Path) -> AsyncIterator[tuple[World, AsyncEngine]]:
    engine = create_async_engine(app_url())
    MockConnector.executed.clear()
    tools = [
        write_tool(PAY, "erp", "pay", input_schema=OPEN, scope_binding={}, confirm_policy="user_confirm",
                   confirm_template="向 {{ args.vendor }} 付款 {{ args.amount }} 元"),
        write_tool(WIRE, "erp", "pay", input_schema=OPEN, scope_binding={}, confirm_policy="approval",
                   approver_role="data_owner"),
    ]  # fmt: skip
    other = {"id": OTHER_USER, "company_id": VN, "home_org_id": VN, "status": "active", "clearance": 2,
             "org_ids": [VN], "data_scope_org_ids": [VN], "roles": []}  # fmt: skip
    w = World(
        tmp_path,
        tools,
        [connector("erp", "mock", {"operations": {"pay": {"response": {"paid": True}, "write": True}}})],
        runtime_engine=engine,
        static_token=STATIC,
        consumer_jwk=CONSUMER_KEY.public_jwk(),
        extra_users={"emp-2": other},
    )
    await w.ready()
    yield w, engine
    await engine.dispose()


def client(w: World) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://dp")


AUTH = {"Authorization": f"Bearer {STATIC}"}


async def request_pay(c: httpx.AsyncClient, key: str = "pay-1", args: dict[str, Any] | None = None) -> dict[str, Any]:
    h = {**AUTH, "X-User-Assertion": assertion(), "Idempotency-Key": key}
    r = await c.post(f"/api/v1/invoke/{PAY}", json={"args": args or ARGS}, headers=h)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_pending_confirmation_and_single_execution(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with client(w) as c:
        r = await request_pay(c)
        assert r["status"] == "pending_confirm" and r["error"]["code"] == "CONFIRMATION_REQUIRED"
        cid = r["next_action"]["confirmation_id"]
        assert r["next_action"]["message"] == "向 ACME 付款 800 元"  # 文案来自 Tool Spec 模板，不来自模型
        assert MockConnector.executed == {}
        # 同一幂等键重试：拿到同一个确认，不新建
        again = await request_pay(c)
        assert again["execution_id"] == r["execution_id"] and again["next_action"]["confirmation_id"] == cid
        polled = (await c.get(f"/api/v1/executions/{r['execution_id']}", headers=AUTH)).json()
        assert polled["status"] == "pending_confirm"

        ok = await c.post(f"/api/v1/confirmations/{cid}/confirm", json={"assertion": assertion()}, headers=AUTH)
        assert ok.status_code == 200 and ok.json()["status"] == "succeeded", ok.text
        assert ok.json()["execution_id"] == r["execution_id"]
        # 再确认一次：不再执行，返回同一结果
        dup = await c.post(f"/api/v1/confirmations/{cid}/confirm", json={"assertion": assertion()}, headers=AUTH)
        assert dup.json()["execution_id"] == r["execution_id"] and dup.json()["status"] == "succeeded"
        assert list(MockConnector.executed) == [f"erp:{cid}"]
        done = (await c.get(f"/api/v1/executions/{r['execution_id']}", headers=AUTH)).json()
        assert done["status"] == "succeeded"


async def test_concurrent_confirmations_execute_once(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with client(w) as c:
        cid = (await request_pay(c, "pay-c"))["next_action"]["confirmation_id"]
    results = await asyncio.gather(
        *(w.runtime.execute_confirmation(cid, by="user:x", via="callback") for _ in range(6)), return_exceptions=True
    )
    ok = [r for r in results if isinstance(r, dict)]
    assert ok and len({r["execution_id"] for r in ok}) == 1
    assert list(MockConnector.executed) == [f"erp:{cid}"]


async def test_assertion_rules(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    async with client(w) as c:
        cid = (await request_pay(c, "pay-a"))["next_action"]["confirmation_id"]
        url = f"/api/v1/confirmations/{cid}/confirm"
        no_assertion = await c.post(url, json={}, headers=AUTH)
        assert no_assertion.json()["code"] == "ASSERTION_INVALID"
        other_user = await c.post(url, json={"assertion": assertion("emp-2")}, headers=AUTH)
        assert other_user.status_code == 403 and other_user.json()["code"] == "PERMISSION_DENIED"
        used = assertion()
        await c.post(f"/api/v1/invoke/{PAY}", json={"args": ARGS}, headers={**AUTH, "X-User-Assertion": used,
                                                                            "Idempotency-Key": "pay-a"})  # fmt: skip
        replay = await c.post(url, json={"assertion": used}, headers=AUTH)
        assert replay.json()["code"] == "ASSERTION_REPLAYED"
        # 确认对象创建之前签发的断言无效
        old = await c.post(f"/api/v1/invoke/{PAY}", json={"args": {**ARGS, "amount": 1}},
                           headers={**AUTH, "X-User-Assertion": assertion(), "Idempotency-Key": "pay-old"})  # fmt: skip
        stale = assertion()
        await asyncio.sleep(1.1)  # iat 是整秒
        cid2 = (await request_pay(c, "pay-new", {**ARGS, "amount": 2}))["next_action"]["confirmation_id"]
        early = await c.post(f"/api/v1/confirmations/{cid2}/confirm", json={"assertion": stale}, headers=AUTH)
        assert early.json()["code"] == "ASSERTION_INVALID", early.text
        assert old.status_code == 200
        assert MockConnector.executed == {}


async def test_tampered_parameters_are_rejected(world: tuple[World, AsyncEngine]) -> None:
    w, engine = world
    async with client(w) as c:
        cid = (await request_pay(c, "pay-t"))["next_action"]["confirmation_id"]
        url = f"/api/v1/confirmations/{cid}/confirm"
        # 1) 消费方声明的确认参数与冻结内容不一致
        r = await c.post(url, json={"assertion": assertion(), "args": {**ARGS, "amount": 80000}}, headers=AUTH)
        assert r.status_code == 409 and r.json()["code"] == "CONFIRMATION_MISMATCH"
        # 2) 存储中的冻结参数被改动（哈希不再匹配）
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            await conn.execute(
                text("UPDATE exec.confirmation SET args_sha256 = :h WHERE id = :id"), {"id": cid, "h": "0" * 64}
            )
        r = await c.post(url, json={"assertion": assertion()}, headers=AUTH)
        assert r.status_code == 409 and r.json()["code"] == "CONFIRMATION_MISMATCH"
        r = await c.post(url, json={"assertion": assertion()}, headers=AUTH)
        assert r.json()["code"] == "CONFIRMATION_MISMATCH"  # 仍是 pending，但永远不会执行
    assert MockConnector.executed == {}
    row = await w.runtime.confirmations.get(cid)
    assert row is not None and row["status"] == "pending"


async def test_approval_goes_through_platform_todo(world: tuple[World, AsyncEngine]) -> None:
    w, _ = world
    svc = {"Authorization": f"Bearer {get_settings().internal_service_token}"}
    async with client(w) as c:
        h = {**AUTH, "X-User-Assertion": assertion(), "Idempotency-Key": "wire-1"}
        r = (await c.post(f"/api/v1/invoke/{WIRE}", json={"args": ARGS}, headers=h)).json()
        cid = r["next_action"]["confirmation_id"]
        cb = await c.post(f"/api/v1/confirmations/{cid}/confirm", json={"assertion": assertion()}, headers=AUTH)
        assert cb.status_code == 403  # 审批类不能由消费方回调确认
        assert (await c.post("/api/v1/internal/todo", json={}, headers=AUTH)).status_code == 401
        todo = (await c.post("/api/v1/internal/todo", json={"company_ids": [VN]}, headers=svc)).json()
        [item] = todo["confirmations"]
        assert item["kind"] == "approval" and item["approver_role"] == "data_owner" and "args_enc" not in item
        assert (await c.post("/api/v1/internal/todo", json={"company_ids": [KH]}, headers=svc)).json()[
            "confirmations"
        ] == []
        done = await c.post(f"/api/v1/internal/confirmations/{cid}/decide",
                            json={"decision": "approve", "user_id": "owner-1"}, headers=svc)  # fmt: skip
        assert done.json()["status"] == "succeeded"
        row = await w.runtime.confirmations.get(cid)
        assert row is not None and row["status"] == "executed" and row["decided_via"] == "console"


async def test_reject_and_expiry(world: tuple[World, AsyncEngine]) -> None:
    w, engine = world
    async with client(w) as c:
        r = await request_pay(c, "pay-r")
        cid = r["next_action"]["confirmation_id"]
        rej = await c.post(f"/api/v1/confirmations/{cid}/reject", json={"comment": "用户取消"}, headers=AUTH)
        assert rej.json()["status"] == "cancelled"
        late = await c.post(f"/api/v1/confirmations/{cid}/confirm", json={"assertion": assertion()}, headers=AUTH)
        assert late.json()["code"] == "PRECONDITION_FAILED"

        cid2 = (await request_pay(c, "pay-e", {**ARGS, "amount": 3}))["next_action"]["confirmation_id"]
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            await conn.execute(
                text("UPDATE exec.confirmation SET expires_at = :t WHERE id = :id"),
                {"id": cid2, "t": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)},
            )
        exp = await c.post(f"/api/v1/confirmations/{cid2}/confirm", json={"assertion": assertion()}, headers=AUTH)
        assert exp.json()["code"] == "PRECONDITION_FAILED" and "expired" in exp.json()["detail"]
    assert MockConnector.executed == {}
