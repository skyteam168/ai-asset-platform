"""M7a：MCP tasks 扩展与 aip.get_run_status（architecture.md 10.1）。

- tools/list 始终含 aip.get_run_status；initialize / discover 声明 tasks 扩展
- Workflow 作为 MCP 异步工具：未声明 tasks 时返回 run_id；声明后返回 task（id=run_id）
- tasks/get 与 get_run_status 按消费方隔离查询
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
from aip.data_plane.mcp_gateway import GET_RUN_STATUS, TASKS_EXT
from aip.data_plane.workflow.engine import WorkflowEngine
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from aip.libs.workflow.dsl import validate
from iam_seed import app_url
from m3_world import USER, World, connector, tool

pytestmark = pytest.mark.infra

STATIC = "aipk_m7_mcp_static"
CONSUMER_KEY = SigningKey.generate()
WF = "agent.ops.shift_handoff"
READ = "ops.mes.query_shift"
TASKS_META = {TASKS_EXT: {}}


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


def flow() -> dict[str, Any]:
    """入口即人工任务：Worker tick 后进入 waiting，便于测异步 MCP。"""
    return {
        "name": "shift_handoff",
        "capability_id": WF,
        "version": "1.0.0",
        "description": "班次交接审批",
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
        "budget": {"max_steps": 10, "max_llm_calls": 0, "timeout_s": 600},
        "nodes": [
            {
                "id": "n1",
                "type": "human_task",
                "kind": "confirm",
                "assignee": "initiator",
                "summary": "班次交接需本人确认",
                "next": "n2",
            },
            {"id": "n2", "type": "end", "output": {"note": "$input.note", "ok": True}},
        ],
    }


@pytest.fixture
async def engine(db: Database) -> AsyncIterator[AsyncEngine]:
    e = create_async_engine(app_url())
    yield e
    await e.dispose()


async def make_world(tmp: pathlib.Path, engine: AsyncEngine) -> World:
    MockConnector.executed.clear()
    dsl = flow()
    assert validate(dsl, lambda *_: {"risk_level": "read", "input_schema": {"type": "object"}}) == []
    w = World(
        tmp,
        [tool(READ, "mes", "q", input_schema={"type": "object"}, output_schema={"type": "object"})],
        [connector("mes", "mock", {"operations": {"q": {"response": {}}}})],
        workflows=[dsl],
        runtime_engine=engine,
        static_token=STATIC,
        consumer_jwk=CONSUMER_KEY.public_jwk(),
    )
    await w.ready()
    w.runtime.workflow_sync_wait_s = 0
    wf: WorkflowEngine = w.app.state.workflows
    wf.lease_s, wf.poll_s, wf.reconcile_base_s, wf.stale_s = 2.0, 0.05, 0.05, 0.5
    return w


async def park(w: World, run_id: str) -> dict[str, Any]:
    """推进到人工等待。"""
    wf: WorkflowEngine = w.app.state.workflows
    for _ in range(20):
        while await wf.tick():
            pass
        view = await wf.view(run_id)
        if view["status"] == "waiting" and view["pending_tasks"]:
            return view
    raise AssertionError(f"Run 未进入 waiting: {await wf.view(run_id)}")


async def test_capabilities_and_builtin_tool(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await make_world(tmp_path, engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        init = await c.post(
            "/mcp",
            headers=headers(),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        )
        caps = init.json()["result"]["capabilities"]
        assert caps["extensions"][TASKS_EXT] is not None

        disc = await c.post("/mcp", headers=headers(), json=rpc("server/discover"))
        assert disc.json()["result"]["capabilities"]["extensions"][TASKS_EXT] is not None

        listed = await c.post("/mcp", headers=headers(), json=rpc("tools/list"))
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert GET_RUN_STATUS in names
        assert WF in names
        assert READ in names
        builtin = next(t for t in listed.json()["result"]["tools"] if t["name"] == GET_RUN_STATUS)
        assert builtin["annotations"]["readOnlyHint"] is True


async def test_async_call_without_tasks_returns_run_id(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await make_world(tmp_path, engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        r = await c.post(
            "/mcp",
            headers=headers(),
            json=rpc("tools/call", {"name": WF, "arguments": {"note": "handoff-1"}}),
        )
        body = r.json()["result"]
        assert body["isError"] is False
        assert "task" not in body
        sc = body["structuredContent"]
        assert sc["status"] == "running" and sc["run_id"]
        assert body["_meta"]["aip.internal/run_id"] == sc["run_id"]
        assert body["_meta"]["aip.internal/async"] is True

        await park(w, sc["run_id"])
        st = await c.post(
            "/mcp",
            headers=headers(),
            json=rpc(
                "tools/call",
                {"name": GET_RUN_STATUS, "arguments": {"run_id": sc["run_id"]}},
                rid=2,
            ),
        )
        view = st.json()["result"]["structuredContent"]
        assert view["run_id"] == sc["run_id"]
        assert view["status"] == "waiting"
        assert view["pending_tasks"]


async def test_async_call_with_tasks_and_tasks_get(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await make_world(tmp_path, engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        r = await c.post(
            "/mcp",
            headers=headers(),
            json=rpc(
                "tools/call",
                {"name": WF, "arguments": {"note": "handoff-2"}},
                extra_meta=TASKS_META,
            ),
        )
        body = r.json()["result"]
        assert body["resultType"] == "task"
        assert body["task"]["id"]
        assert body["task"]["status"] == "working"
        run_id = body["task"]["id"]
        assert body["_meta"]["aip.internal/task_id"] == run_id

        parked = await park(w, run_id)
        got = await c.post(
            "/mcp",
            headers=headers(),
            json=rpc("tasks/get", {"taskId": run_id}, rid=2),
        )
        task = got.json()["result"]["task"]
        assert task["id"] == run_id
        assert task["status"] == "input_required"

        wf: WorkflowEngine = w.app.state.workflows
        await wf.decide_task(parked["pending_tasks"][0]["id"], decision="approve", user_id=USER)
        for _ in range(20):
            while await wf.tick():
                pass
            settled = await wf.view(run_id)
            if settled["status"] == "succeeded":
                break
        assert settled["status"] == "succeeded", settled

        done = await c.post(
            "/mcp",
            headers=headers(),
            json=rpc("tasks/get", {"taskId": run_id}, rid=3),
        )
        res = done.json()["result"]
        assert res["task"]["status"] == "completed"
        assert res["result"]["status"] == "succeeded"
        assert res["result"]["data"]["ok"] is True


async def test_get_run_status_unknown_run(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await make_world(tmp_path, engine)
    r = await w.runtime.invoke(w.principal(), WF, {"note": "x"}, idempotency_key="k1")
    run_id = r["run_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
        bad = await c.post(
            "/mcp",
            headers=headers(),
            json=rpc(
                "tools/call",
                {"name": GET_RUN_STATUS, "arguments": {"run_id": "00000000-0000-0000-0000-000000000001"}},
            ),
        )
        assert bad.json()["error"]["message"] == "Unknown run"

        ok = await c.post(
            "/mcp",
            headers=headers(),
            json=rpc("tools/call", {"name": GET_RUN_STATUS, "arguments": {"run_id": run_id}}, rid=2),
        )
        assert ok.json()["result"]["structuredContent"]["run_id"] == run_id
