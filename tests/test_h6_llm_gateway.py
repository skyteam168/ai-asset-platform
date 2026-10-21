"""H6：LLM Gateway — 白名单、日 token 预算、规则脱敏、OpenAI 兼容。"""

from __future__ import annotations

import copy
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database
from aip.data_plane.llm.allowlist import MemoryAllowlistStore
from aip.data_plane.llm.budget import MemoryBudgetStore
from aip.data_plane.llm.gateway import LlmGateway
from aip.data_plane.llm.redact import redact_text
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import PASSWORD, Seed, app_url
from m3_world import VN
from test_m5_workflow import PO_REVIEW, make, settle, start

PROFILE = {
    "model": "gpt-test",
    "base_url": "https://llm.example.internal",
    "temperature": 0,
    "max_tokens": 256,
    "deployment": "local-vllm",
    "api_path": "/v1/chat/completions",
}


def _mock_transport(
    captured: list[dict[str, Any]], content: str = '{"risk_level":"low"}', tokens: int = 42
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": tokens},
            },
        )

    return httpx.MockTransport(handler)


async def _gateway(
    *,
    company_id: uuid.UUID,
    allow: list[str] | None = None,
    daily: int = 10_000,
    captured: list[dict[str, Any]] | None = None,
    audit: list[dict[str, Any]] | None = None,
    content: str = '{"risk_level":"low"}',
) -> LlmGateway:
    al = MemoryAllowlistStore()
    bd = MemoryBudgetStore()
    if allow is not None:
        await al.set_names(company_id, allow)
    await bd.set_daily_limit(company_id, daily)
    cap = captured if captured is not None else []
    return LlmGateway(
        allowlist=al,
        budget=bd,
        resolve_profile=lambda _ref, _env: PROFILE,
        env="test",
        transport=_mock_transport(cap, content=content),
        audit_hook=(audit.append if audit is not None else None),
    )


SCHEMA = {
    "type": "object",
    "properties": {"risk_level": {"enum": ["low", "high"]}},
    "required": ["risk_level"],
}


@pytest.fixture
async def engine(db: Database) -> AsyncIterator[AsyncEngine]:
    e = create_async_engine(app_url())
    yield e
    await e.dispose()


@pytest.mark.asyncio
async def test_allowlist_denies() -> None:
    cid = uuid.uuid4()
    gw = await _gateway(company_id=cid, allow=[])
    with pytest.raises(PlatformError) as ei:
        await gw.complete(
            model_profile="mp_extract@1.0.0",
            prompt="x",
            variables={},
            output_schema=SCHEMA,
            company_id=str(cid),
        )
    assert ei.value.code == ErrorCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_budget_exceeded() -> None:
    cid = uuid.uuid4()
    gw = await _gateway(company_id=cid, allow=["mp_extract"], daily=10)
    await gw.budget.reserve_and_add(cid, dt.datetime.now(dt.UTC).date(), 10)
    with pytest.raises(PlatformError) as ei:
        await gw.complete(
            model_profile="mp_extract@1.0.0",
            prompt="hello",
            variables={},
            output_schema=SCHEMA,
            company_id=str(cid),
        )
    assert ei.value.code == ErrorCode.RATE_LIMITED


@pytest.mark.asyncio
async def test_redact_phone_in_request_and_audit() -> None:
    cid = uuid.uuid4()
    captured: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    gw = await _gateway(company_id=cid, allow=["mp_extract"], captured=captured, audit=audit)
    phone = "13812345678"
    res = await gw.complete(
        model_profile="mp_extract@1.0.0",
        prompt="联系人手机 {{ phone }}",
        variables={"phone": phone},
        output_schema=SCHEMA,
        company_id=str(cid),
    )
    assert res.data["risk_level"] == "low" and res.tokens == 42
    user_msg = captured[0]["messages"][1]["content"]
    assert phone not in user_msg
    assert "[REDACTED_PHONE]" in user_msg
    assert phone not in json.dumps(audit)
    assert phone not in audit[0]["prompt_preview"]


@pytest.mark.asyncio
async def test_mock_complete_structured() -> None:
    cid = uuid.uuid4()
    gw = await _gateway(company_id=cid, allow=["mp_extract"], content='{"risk_level":"high","extra":1}')
    res = await gw.complete(
        model_profile="mp_extract@1.0.0",
        prompt="p",
        variables={},
        output_schema=SCHEMA,
        company_id=str(cid),
    )
    assert res.data == {"risk_level": "high"}


@pytest.mark.asyncio
async def test_workflow_llm_via_gateway(tmp_path: Any, engine: AsyncEngine) -> None:
    company = uuid.UUID(VN)
    captured: list[dict[str, Any]] = []
    al = MemoryAllowlistStore()
    bd = MemoryBudgetStore()
    await al.set_names(company, ["mp_extract"])
    await bd.set_daily_limit(company, 50_000)
    gw = LlmGateway(
        allowlist=al,
        budget=bd,
        resolve_profile=lambda _r, _e: PROFILE,
        env="test",
        transport=_mock_transport(captured, content='{"risk_level":"low"}'),
    )
    w = await make(tmp_path, engine, [copy.deepcopy(PO_REVIEW)], gw)
    run_id = await start(w, {"po": "PO-1"})
    view = await settle(w, run_id, {"succeeded", "waiting_human", "failed"})
    assert captured, f"llm 节点应经 Gateway 调用上游；status={view['status']}"
    assert view["status"] in ("succeeded", "waiting_human")
    assert captured[0]["model"] == "gpt-test"


def test_redact_helpers() -> None:
    s = redact_text("a@b.com 13812345678 sk-abcdefghijklmnopqrstuv Bearer abcdefghijklmnopqrstuvwxyz12")
    assert "[REDACTED_EMAIL]" in s and "[REDACTED_PHONE]" in s
    assert "[REDACTED_SECRET]" in s and "[REDACTED_TOKEN]" in s


async def _login(cp: httpx.AsyncClient, seed: Seed, name: str = "admin") -> dict[str, str]:
    r = await cp.post(
        "/api/v1/auth/login",
        json={"login_name": name, "password": PASSWORD, "totp": seed.code(name)},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


@pytest.mark.asyncio
async def test_control_plane_llm_config_http(db: Database, seeded: Seed) -> None:
    transport = httpx.ASGITransport(app=create_cp(db))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cp:
        h = await _login(cp, seeded)
        cid = str(seeded.vn)
        r = await cp.put(
            f"/api/v1/companies/{cid}/llm-allowlist", headers=h, json={"names": ["mp_extract", "mp_chat"]}
        )
        assert r.status_code == 200, r.text
        assert r.json()["names"] == ["mp_chat", "mp_extract"]
        r = await cp.get(f"/api/v1/companies/{cid}/llm-allowlist", headers=h)
        assert r.json()["names"] == ["mp_chat", "mp_extract"]
        r = await cp.put(f"/api/v1/companies/{cid}/llm-budget", headers=h, json={"daily_tokens": 1000})
        assert r.status_code == 200 and r.json()["daily_tokens"] == 1000
        r = await cp.get(f"/api/v1/companies/{cid}/llm-usage", headers=h)
        assert r.status_code == 200
        assert r.json()["tokens_used"] == 0 and r.json()["daily_tokens"] == 1000
