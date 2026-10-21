"""M7g：A2A 1.0 最小出口（Agent Card + SendMessage / GetTask）。

验收：Card 仅列 exposure 含 a2a 的已授权 Workflow；SendMessage 启动并返回 task.id=run_id；
GetTask 可查到完成态；未认证 401。
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
from aip.data_plane.a2a_gateway import STATE_COMPLETED
from aip.data_plane.workflow.engine import WorkflowEngine
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from aip.libs.workflow.dsl import validate
from iam_seed import app_url
from m3_world import World, connector, tool

pytestmark = pytest.mark.infra

STATIC = "aipk_m7g_a2a"
CONSUMER_KEY = SigningKey.generate()
WF = "agent.ops.a2a_echo"
READ = "ops.mes.query_shift"


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


def flow() -> dict[str, Any]:
    """入口即 end：可同步完成，便于验收 GetTask → COMPLETED。"""
    return {
        "name": "a2a_echo",
        "capability_id": WF,
        "version": "1.0.0",
        "description": "A2A 验收用回显工作流",
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
        "budget": {"max_steps": 5, "max_llm_calls": 0, "timeout_s": 60},
        "nodes": [{"id": "n1", "type": "end", "output": {"note": "$input.note", "ok": True}}],
    }


@pytest.fixture
async def engine(db: Database) -> AsyncIterator[AsyncEngine]:
    e = create_async_engine(app_url())
    yield e
    await e.dispose()


async def make_world(tmp: pathlib.Path, engine: AsyncEngine, *, exposure: list[str]) -> World:
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
        exposure=exposure,
    )
    await w.ready()
    w.runtime.workflow_sync_wait_s = 2.0
    wf: WorkflowEngine = w.app.state.workflows
    wf.lease_s, wf.poll_s, wf.reconcile_base_s, wf.stale_s = 2.0, 0.05, 0.05, 0.5
    return w


def rpc(method: str, params: dict[str, Any] | None = None, *, rid: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


async def test_agent_card_requires_auth_and_lists_a2a_workflows(
    tmp_path: pathlib.Path, engine: AsyncEngine
) -> None:
    w = await make_world(tmp_path, engine, exposure=["rest", "a2a"])
    async with w.app.router.lifespan_context(w.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
            anon = await c.get("/.well-known/agent-card.json")
            assert anon.status_code == 401
            assert "resource_metadata" in anon.headers.get("www-authenticate", "")

            card = await c.get("/.well-known/agent-card.json", headers=headers())
            assert card.status_code == 200, card.text
            body = card.json()
            assert body["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
            skills = {s["id"] for s in body["skills"]}
            assert skills == {WF}
            assert READ not in skills  # Tool 不经 A2A


async def test_agent_card_hides_workflow_without_a2a_exposure(
    tmp_path: pathlib.Path, engine: AsyncEngine
) -> None:
    w = await make_world(tmp_path, engine, exposure=["rest", "mcp"])
    async with w.app.router.lifespan_context(w.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
            card = await c.get("/.well-known/agent-card.json", headers=headers())
            assert card.status_code == 200
            assert card.json()["skills"] == []


async def test_send_message_and_get_task(tmp_path: pathlib.Path, engine: AsyncEngine) -> None:
    w = await make_world(tmp_path, engine, exposure=["rest", "a2a"])
    async with w.app.router.lifespan_context(w.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://t") as c:
            unauth = await c.post("/a2a", json=rpc("SendMessage", {"message": {}}))
            assert unauth.status_code == 401

            sent = await c.post(
                "/a2a",
                headers={**headers(), "Idempotency-Key": "a2a-1"},
                json=rpc(
                    "SendMessage",
                    {
                        "message": {
                            "role": "user",
                            "parts": [{"kind": "data", "data": {"note": "hello-a2a"}}],
                            "metadata": {"capabilityId": WF},
                        }
                    },
                ),
            )
            assert sent.status_code == 200, sent.text
            task = sent.json()["result"]["task"]
            assert task["id"]
            assert task["status"]["state"] == STATE_COMPLETED
            assert task["artifacts"][0]["parts"][0]["data"]["note"] == "hello-a2a"

            got = await c.post("/a2a", headers=headers(), json=rpc("GetTask", {"id": task["id"]}))
            assert got.status_code == 200, got.text
            again = got.json()["result"]["task"]
            assert again["id"] == task["id"]
            assert again["status"]["state"] == STATE_COMPLETED
