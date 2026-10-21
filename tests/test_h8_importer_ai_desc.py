"""H8：Importer AI 候选描述（经 Gateway / 可注入 LLM）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.importer.service import import_openapi
from aip.data_plane.workflow.llm import LlmResult
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import Seed
from test_m4_gates import _people

ROOT = Path(__file__).resolve().parents[1]
MES = (ROOT / "tests" / "contracts" / "mes-vn01.openapi.yaml").read_text(encoding="utf-8")


class ScriptedEnrichLlm:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def complete(self, **kwargs: Any) -> LlmResult:
        self.calls += 1
        if self.fail:
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "未授权模型")
        return LlmResult(
            data={
                "description": "查询越南工厂生产工单列表，支持按工厂与状态过滤，返回工单号与计划数量。",
                "usage_notes": "需具备该工厂读权限",
                "not_for": ["写入或关闭工单"],
            },
            tokens=10,
        )


@pytest.mark.asyncio
async def test_enrich_off_unchanged(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    llm = ScriptedEnrichLlm()
    async with db.tx(DbScope.system()) as conn:
        out = await import_openapi(
            conn,
            p.dev,
            document=MES,
            connector_name="mes_h8a",
            domain="production",
            system="mes",
            enrich_descriptions=False,
            llm=llm,
        )
    assert llm.calls == 0
    tool = next(t for t in out["tools"] if t["capability_id"].endswith("list_work_orders"))
    assert "usage_notes" not in tool or tool.get("description_lang") != "zh"


@pytest.mark.asyncio
async def test_enrich_rewrites_description(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    llm = ScriptedEnrichLlm()
    async with db.tx(DbScope.system()) as conn:
        out = await import_openapi(
            conn,
            p.dev,
            document=MES,
            connector_name="mes_h8b",
            domain="production",
            system="mes",
            enrich_descriptions=True,
            model_profile="mp_select@1.0.0",
            owner_org_id=seeded.hq,
            llm=llm,
        )
    assert llm.calls >= 1
    tool = next(t for t in out["tools"] if "list_work_orders" in t["capability_id"])
    assert "越南工厂" in tool["description"]
    assert tool.get("description_lang") == "zh"
    assert tool.get("usage_notes")
    assert tool.get("not_for")


@pytest.mark.asyncio
async def test_enrich_requires_model_profile(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as ei:
            await import_openapi(
                conn,
                p.dev,
                document=MES,
                connector_name="mes_h8c",
                enrich_descriptions=True,
                model_profile=None,
                owner_org_id=seeded.hq,
                llm=ScriptedEnrichLlm(),
            )
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_enrich_fallback_on_llm_deny(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    llm = ScriptedEnrichLlm(fail=True)
    async with db.tx(DbScope.system()) as conn:
        baseline = await import_openapi(
            conn, p.dev, document=MES, connector_name="mes_h8d0", domain="production", system="mes"
        )
        out = await import_openapi(
            conn,
            p.dev,
            document=MES,
            connector_name="mes_h8d",
            domain="production",
            system="mes",
            enrich_descriptions=True,
            model_profile="mp_select@1.0.0",
            owner_org_id=seeded.hq,
            llm=llm,
        )
    base = next(t for t in baseline["tools"] if "list_work_orders" in t["capability_id"])
    tool = next(t for t in out["tools"] if "list_work_orders" in t["capability_id"])
    assert tool["description"] == base["description"]
    assert any("AI 描述失败" in w for w in out["warnings"])
