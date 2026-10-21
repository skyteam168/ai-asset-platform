"""H30：Skill 指导效果评测（复用选择评测 G2，注入 SKILL.md 正文）。"""

from __future__ import annotations

import json
import uuid

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import package as pkg
from aip.control_plane.skill import service as skill
from aip.control_plane.testlab.selection_eval import build_prompt
from aip.libs.tokens.keys import SigningKey
from iam_seed import Seed
from m2_fixtures import promote, tool_spec
from test_h7_selection_eval import MP, ScriptedLlm, _cases
from test_m4_gates import SECRETS, _people

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()


def test_build_prompt_includes_skill_guidance() -> None:
    p = build_prompt(
        "查产品",
        [{"capability_id": "nike.sample.pc_12", "description": "产品列表"}],
        skill_guidance="需要产品列表时调用 nike.sample.pc_12。",
    )
    assert "Skill 说明书" in p
    assert "nike.sample.pc_12" in p


async def _publish_tool(db: Database, s: Seed, p: object, cap: str) -> str:
    spec = tool_spec(capability_id=cap, name=cap.split(".")[-1])
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="tool", name=cap, owner_org_id=s.hq, classification=2  # type: ignore[attr-defined]
        )
        vid = await registry.create_version(conn, p.dev, aid, "1.0.0", spec)  # type: ignore[attr-defined]
        await promote(conn, p.dev, vid)  # type: ignore[attr-defined]
    return f"{cap}@1.0.0"


async def test_skill_g2_pass_with_scripted_llm(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = f"ops.h30.pass_{uuid.uuid4().hex[:6]}"
    ref = await _publish_tool(db, seeded, p, cap)
    cases = _cases(cap)
    llm = ScriptedLlm([c["expect_capability_id"] for c in cases])
    sname = f"h30-skill-{uuid.uuid4().hex[:6]}"
    zip_bytes = pkg.make_zip(
        {
            "SKILL.md": (
                f"---\nname: {sname}\n"
                f"description: 指导 Agent 在演示场景选择正确 Tool 的 Skill。\n"
                f"---\n\n# 指引\n\n查询时优先调用 `{cap}`。\n"
            ).encode(),
            "skill.lock.json": json.dumps({"tools": [ref]}).encode(),
        }
    )
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name=f"skill.{sname.replace('-', '_')}", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": sname, "description": "指导 Agent 在演示场景选择正确 Tool 的 Skill。"},
        )
        await skill.attach_package(conn, p.dev, vid, zip_bytes, key=KEY)
        await gates.put_selection_eval(conn, p.dev, vid, model_profiles=[MP], cases=cases)
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["指导选型"]})
        assert (await gates.submit(conn, p.dev, vid))["passed"]
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS, llm=llm)
        assert lab["passed"] is True, lab
        assert lab["selection_eval"]["models"][0]["top1_hit_rate"] == 1.0


async def test_skill_g2_fail_without_eval_when_tools_locked(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    cap = f"ops.h30.noeval_{uuid.uuid4().hex[:6]}"
    ref = await _publish_tool(db, seeded, p, cap)
    sname = f"h30-noeval-{uuid.uuid4().hex[:6]}"
    zip_bytes = pkg.make_zip(
        {
            "SKILL.md": (
                f"---\nname: {sname}\ndescription: 有 Tool 引用但未配置评测集的 Skill。\n---\n\n# x\n"
            ).encode(),
            "skill.lock.json": json.dumps({"tools": [ref]}).encode(),
        }
    )
    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name=f"skill.{sname.replace('-', '_')}", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": sname, "description": "有 Tool 引用但未配置评测集的 Skill。"},
        )
        await skill.attach_package(conn, p.dev, vid, zip_bytes, key=KEY)
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["x"]})
        assert (await gates.submit(conn, p.dev, vid))["passed"]
        lab = await gates.run_lab(conn, p.dev, vid, secrets=SECRETS, llm=ScriptedLlm([]))
        assert lab["passed"] is False
        assert any("20" in p for p in lab.get("selection_eval", {}).get("problems", []))
