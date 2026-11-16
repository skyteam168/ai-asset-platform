"""Skill AI 草稿：勾选 Tool → LLM Gateway → 候选 name/description/body（Mock LLM）。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database, DbScope
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import generate as skill_gen
from aip.control_plane.skill import schemas as skill_out
from aip.data_plane.workflow.llm import LlmResult
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import Seed
from m2_fixtures import promote, tool_spec
from test_h22_response_models import _route
from test_m4_gates import _people


class ScriptedSkillLlm:
    def __init__(self, *, with_script: bool = False, short: bool = False) -> None:
        self.calls = 0
        self.last_prompt = ""
        self.with_script = with_script
        self.short = short

    async def complete(self, **kwargs: Any) -> LlmResult:
        self.calls += 1
        self.last_prompt = str(kwargs.get("prompt") or "")
        if self.short:
            return LlmResult(data={"name": "x", "description": "太短", "body": "短"}, tokens=2)
        body = (
            "# 查询工单\n\n```python\nprint(1)\n```\n"
            if self.with_script
            else (
                "## 何时使用\n\n需要按工厂汇总在制工单时启用。\n\n"
                "## 步骤\n\n1. 确认工厂范围。\n2. 调用列出的 Tool 并汇总结果。\n\n"
                "## 注意事项\n\n- 只读授权范围内数据。\n"
            )
        )
        return LlmResult(
            data={
                "name": "Query Work Orders!!",
                "description": "指导模型在夜班场景按工厂查询在制工单并汇总状态。",
                "body": body,
            },
            tokens=30,
        )


async def _publish_tool(db: Database, s: Seed, p: Any, cap: str) -> str:
    spec = tool_spec(capability_id=cap, name=cap.split(".")[-1])
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(conn, p.dev, type_="tool", name=cap, owner_org_id=s.hq, classification=2)
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)
        await promote(conn, p.dev, vid)
    return f"{cap}@1.0.0"


async def _skill_draft(db: Database, s: Seed, p: Any) -> Any:
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name=f"skill.ai_{uuid.uuid4().hex[:6]}", owner_org_id=s.hq, classification=2
        )
        return await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": "ops-skill", "description": "占位描述足够十个字符以上。"},
        )


@pytest.mark.asyncio
async def test_generate_skill_ai_draft(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = f"ops.ai.draft_{uuid.uuid4().hex[:6]}"
    ref = await _publish_tool(db, seeded, p, cap)
    vid = await _skill_draft(db, seeded, p)
    llm = ScriptedSkillLlm()
    async with db.tx(DbScope.system()) as conn:
        out = await skill_gen.generate_draft(
            conn,
            p.dev,
            version_id=vid,
            tool_refs=[ref],
            scenario="夜班按工厂汇总在制工单",
            model_profile="mp_select@1.0.0",
            hint_name="ops-skill",
            llm=llm,
        )
    assert llm.calls == 1
    assert cap in llm.last_prompt
    assert out["name"] == "query-work-orders"
    assert out["tools"] == [ref]
    assert "候选" in out["warnings"][0]
    assert "何时使用" in out["body"]


@pytest.mark.asyncio
async def test_generate_rejects_empty_tools(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    vid = await _skill_draft(db, seeded, p)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as ei:
            await skill_gen.generate_draft(
                conn,
                p.dev,
                version_id=vid,
                tool_refs=[],
                model_profile="mp_select@1.0.0",
                llm=ScriptedSkillLlm(),
            )
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_generate_rejects_script_body(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = f"ops.ai.script_{uuid.uuid4().hex[:6]}"
    ref = await _publish_tool(db, seeded, p, cap)
    vid = await _skill_draft(db, seeded, p)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as ei:
            await skill_gen.generate_draft(
                conn,
                p.dev,
                version_id=vid,
                tool_refs=[ref],
                model_profile="mp_select@1.0.0",
                llm=ScriptedSkillLlm(with_script=True),
            )
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT
    assert "脚本" in ei.value.detail


def test_skill_ai_draft_route_response_model() -> None:
    app = create_cp()
    assert _route(app, "generateSkillAiDraft").response_model is skill_out.SkillAiDraft
