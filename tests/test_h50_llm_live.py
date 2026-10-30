"""G4.3 / H50：LLM Gateway GenAI span；真上游 chat（无密钥 skip）。"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any

import pytest

from aip.data_plane.llm.allowlist import MemoryAllowlistStore
from aip.data_plane.llm.budget import MemoryBudgetStore
from aip.data_plane.llm.gateway import LlmGateway
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.otel import memory_exporter, setup_tracing
from test_h6_llm_gateway import SCHEMA, _gateway


@pytest.mark.asyncio
async def test_gateway_emits_gen_ai_span_attrs() -> None:
    setup_tracing(service_name="aip-llm-test", exporter="memory", force=True)
    exp = memory_exporter()
    assert exp is not None
    exp.clear()

    cid = uuid.uuid4()
    gw = await _gateway(company_id=cid, allow=["mp_extract"])
    res = await gw.complete(
        model_profile="mp_extract@1.0.0",
        prompt="p",
        variables={},
        output_schema=SCHEMA,
        company_id=str(cid),
    )
    assert res.tokens == 42
    spans = [s for s in exp.get_finished_spans() if s.name == "gen_ai.chat"]
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("gen_ai.operation.name") == "chat"
    assert attrs.get("gen_ai.request.model") == "gpt-test"
    assert attrs.get("gen_ai.usage.total_tokens") == 42
    assert attrs.get("aip.model_profile") == "mp_extract@1.0.0"
    assert "gen_ai.prompt" not in attrs
    assert "api_key" not in attrs


@pytest.mark.asyncio
async def test_budget_and_redact_still_gate_before_upstream() -> None:
    """预算耗尽时不应出站；脱敏后上游看不到明文手机号。"""
    cid = uuid.uuid4()
    captured: list[dict[str, Any]] = []
    gw = await _gateway(company_id=cid, allow=["mp_extract"], daily=5, captured=captured)
    await gw.budget.reserve_and_add(cid, dt.datetime.now(dt.UTC).date(), 5)
    with pytest.raises(PlatformError) as ei:
        await gw.complete(
            model_profile="mp_extract@1.0.0",
            prompt="手机 {{ phone }}",
            variables={"phone": "13900001111"},
            output_schema=SCHEMA,
            company_id=str(cid),
        )
    assert ei.value.code == ErrorCode.RATE_LIMITED
    assert captured == []

    gw2 = await _gateway(company_id=cid, allow=["mp_extract"], captured=captured)
    await gw2.complete(
        model_profile="mp_extract@1.0.0",
        prompt="手机 {{ phone }}",
        variables={"phone": "13900001111"},
        output_schema=SCHEMA,
        company_id=str(cid),
    )
    assert "13900001111" not in captured[0]["messages"][1]["content"]
    assert "[REDACTED_PHONE]" in captured[0]["messages"][1]["content"]


@pytest.mark.llm
@pytest.mark.asyncio
async def test_live_llm_chat_completions() -> None:
    """真调用 OpenAI 兼容端点（需 AIP_TEST_LLM_BASE_URL / API_KEY / MODEL）。"""
    base = os.environ.get("AIP_TEST_LLM_BASE_URL", "").strip()
    key = os.environ.get("AIP_TEST_LLM_API_KEY", "").strip()
    model = os.environ.get("AIP_TEST_LLM_MODEL", "").strip()
    if not base or not key or not model:
        pytest.skip("AIP_TEST_LLM_BASE_URL / AIP_TEST_LLM_API_KEY / AIP_TEST_LLM_MODEL not set")

    cid = uuid.uuid4()
    al = MemoryAllowlistStore()
    bd = MemoryBudgetStore()
    await al.set_names(cid, ["mp_live"])
    await bd.set_daily_limit(cid, 50_000)
    profile = {
        "model": model,
        "base_url": base.rstrip("/"),
        "temperature": 0,
        "max_tokens": 128,
        "deployment": "live-test",
        "api_path": os.environ.get("AIP_TEST_LLM_API_PATH", "/v1/chat/completions"),
    }
    gw = LlmGateway(
        allowlist=al,
        budget=bd,
        resolve_profile=lambda _r, _e: profile,
        env="test",
        api_key=key,
    )
    res = await gw.complete(
        model_profile="mp_live@1.0.0",
        prompt='Reply JSON only: {"risk_level":"low"} for input {{ q }}',
        variables={"q": "ping"},
        output_schema=SCHEMA,
        company_id=str(cid),
    )
    assert res.data.get("risk_level") in ("low", "high")
    assert res.tokens >= 1
    # 勿断言或打印 key
    assert key not in str(res.data)
