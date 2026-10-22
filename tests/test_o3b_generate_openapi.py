"""O3b：经 LLM Gateway 生成候选 OpenAPI（Mock LLM）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.importer import openapi as oa
from aip.control_plane.importer.generate import generate_openapi
from aip.control_plane.importer.service import import_openapi
from aip.data_plane.workflow.llm import LlmResult
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import Seed
from test_m4_gates import _people

SAMPLE_OA = {
    "openapi": "3.0.3",
    "info": {"title": "AI Draft MES", "version": "0.1.0"},
    "paths": {
        "/api/v2/workorders": {
            "get": {
                "operationId": "listWorkOrders",
                "parameters": [
                    {
                        "name": "plant",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "integer"},
                                        "data": {
                                            "type": "object",
                                            "properties": {
                                                "items": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "object",
                                                        "properties": {
                                                            "woNo": {"type": "string"},
                                                        },
                                                    },
                                                }
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


class ScriptedGenLlm:
    def __init__(self, *, bad: bool = False, binary_note: bool = False) -> None:
        self.calls = 0
        self.bad = bad
        self.last_prompt = ""

    async def complete(self, **kwargs: Any) -> LlmResult:
        self.calls += 1
        self.last_prompt = str(kwargs.get("prompt") or "")
        if self.bad:
            return LlmResult(data={"document_json": "{not-json"}, tokens=3)
        return LlmResult(
            data={
                "document_json": json.dumps(SAMPLE_OA, ensure_ascii=False),
                "title": "AI Draft MES",
                "notes": ["参数 plant 需与工厂编码一致"],
            },
            tokens=40,
        )


@pytest.mark.asyncio
async def test_generate_openapi_then_preview(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    llm = ScriptedGenLlm()
    async with db.tx(DbScope.system()) as conn:
        out = await generate_openapi(
            conn,
            p.dev,
            description="越南 MES 需要按工厂查询在制工单列表，返回工单号。",
            model_profile="mp_select@1.0.0",
            extras=["plant | string | 工厂编码\nWO-1 | open"],
            owner_org_id=seeded.hq,
            domain_hint="production",
            system_hint="mes",
            llm=llm,
        )
    assert llm.calls == 1
    assert "附录文本" in llm.last_prompt
    assert out["format"] == "openapi"
    assert out["operation_count"] >= 1
    assert "候选" in out["warnings"][0]
    doc = oa.load_document(out["document"])
    ops, _ = oa.parse_operations(doc)
    assert any(o["operation"] == "list_work_orders" for o in ops)

    async with db.tx(DbScope.system()) as conn:
        preview = await import_openapi(
            conn,
            p.dev,
            document=out["document"],
            connector_name="mes_o3b",
            domain="production",
            system="mes",
            persist=False,
        )
    assert any(t["capability_id"].endswith("list_work_orders") for t in preview["tools"])


@pytest.mark.asyncio
async def test_generate_rejects_short_description(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as ei:
            await generate_openapi(
                conn,
                p.dev,
                description="太短",
                model_profile="mp_select@1.0.0",
                owner_org_id=seeded.hq,
                llm=ScriptedGenLlm(),
            )
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_generate_rejects_binary_extra(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as ei:
            await generate_openapi(
                conn,
                p.dev,
                description="越南 MES 需要按工厂查询在制工单列表，返回工单号。",
                model_profile="mp_select@1.0.0",
                extras=["ok text", "\x00\x01\x02binary"],
                owner_org_id=seeded.hq,
                llm=ScriptedGenLlm(),
            )
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT
    assert "图片" in ei.value.detail or "纯文本" in ei.value.detail
