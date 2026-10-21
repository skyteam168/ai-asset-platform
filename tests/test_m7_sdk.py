"""M7e：官方 Python SDK（catalog / invoke / 令牌交换）与可选 LangChain 适配。"""

from __future__ import annotations

import datetime as dt
import pathlib
import secrets

import httpx
import jwt
import pytest

from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey
from aip.sdk import Client, SdkError
from m3_world import World, connector, tool

pytestmark = pytest.mark.infra

CAP = "production.mes.query_work_orders"
STATIC = "aipk_m7e_sdk"
ARGS = {"factory_id": "F001", "status": "open"}
CONSUMER_KEY = SigningKey.generate()


def assertion(sub: str = "emp-1", *, aud: str | None = None) -> str:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    claims = {
        "iss": "vn-ai",
        "sub": sub,
        "aud": aud or get_settings().mcp_resource,
        "iat": now,
        "exp": now + 120,
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(claims, CONSUMER_KEY.private, algorithm="ES256", headers={"kid": CONSUMER_KEY.kid})


@pytest.fixture
async def world(tmp_path: pathlib.Path) -> World:
    w = World(
        tmp_path,
        [tool(CAP, "mes", "q")],
        [connector("mes", "mock", {"operations": {"q": {"response": [{"wo_no": "W1", "qty": 1, "status": "open"}]}}})],
        static_token=STATIC,
        consumer_jwk=CONSUMER_KEY.public_jwk(),
        exposure=["rest", "mcp"],
    )
    await w.ready()
    return w


def _client(w: World) -> Client:
    return Client(
        "http://dp",
        transport=httpx.ASGITransport(app=w.app),
        static_token=STATIC,
        client_id="vn-ai",
    )


async def test_sdk_catalog_invoke_and_execution(world: World) -> None:
    async with _client(world) as c:
        cat = await c.catalog()
        caps = {i["capability_id"] for i in cat["items"]}
        assert CAP in caps
        item = next(i for i in cat["items"] if i["capability_id"] == CAP)
        assert "factory_id" in (item.get("input_schema") or {}).get("properties", {})

        out = await c.invoke(CAP, ARGS, idempotency_key="m7e-1")
        assert out["status"] == "succeeded", out
        eid = out["execution_id"]
        got = await c.get_execution(eid)
        assert got["status"] == "succeeded" and got["execution_id"] == eid


async def test_sdk_exchange_token(world: World) -> None:
    async with _client(world) as c:
        tok = await c.exchange_token(assertion(aud=get_settings().token_endpoint_aud))
        assert tok["token_type"] == "Bearer" and c.access_token
        cat = await c.catalog()
        assert CAP in {i["capability_id"] for i in cat["items"]}


async def test_sdk_error_problem_details(world: World) -> None:
    async with _client(world) as c:
        with pytest.raises(SdkError) as ei:
            await c.get_execution("00000000-0000-0000-0000-000000000000")
        assert ei.value.status == 404
        assert ei.value.code == "NOT_FOUND"


async def test_sdk_as_langchain_tools(world: World) -> None:
    pytest.importorskip("langchain_core")
    async with _client(world) as c:
        tools = await c.as_langchain_tools(names={CAP})
        assert len(tools) == 1 and tools[0].name == CAP
        result = await tools[0].ainvoke(ARGS)
        assert isinstance(result, dict)
        assert result["status"] == "succeeded"
