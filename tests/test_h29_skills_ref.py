"""H29：G0 接入官方 skills-ref validate。"""

from __future__ import annotations

import json

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.gates import service as gates
from aip.control_plane.registry import service as registry
from aip.control_plane.skill import package as pkg
from aip.control_plane.skill import service as skill
from aip.control_plane.skill import skills_ref
from aip.libs.tokens.keys import SigningKey
from iam_seed import Seed
from test_m4_gates import People, _people

pytestmark = pytest.mark.infra
KEY = SigningKey.generate()


def _zip(name: str, description: str, *, bad_name: bool = False) -> bytes:
    nm = "BAD NAME" if bad_name else name
    files = {
        "SKILL.md": (
            f"---\nname: {nm}\ndescription: {description}\n---\n\n# {nm}\n\n使用平台 Tool 查询数据。\n"
        ).encode(),
        "skill.lock.json": json.dumps({"tools": []}).encode(),
    }
    return pkg.make_zip(files)


def test_validate_skill_files_ok() -> None:
    files = pkg.unpack_zip(
        _zip("nike-sample-home", "指导 Agent 查询 Nike 打样首页统计与产品列表。")
    )
    assert skills_ref.validate_skill_files(files) == []


def test_validate_skill_files_rejects_invalid_name() -> None:
    files = pkg.unpack_zip(_zip("x", "描述长度足够十个字符以上用于校验。", bad_name=True))
    problems = skills_ref.validate_skill_files(files)
    assert problems and any("skills-ref" in p for p in problems)


def _zip_underscore() -> bytes:
    """平台 NAME_RE 允许下划线，skills-ref 仅允许连字符 → 用于 G0 失败路径。"""
    return pkg.make_zip(
        {
            "SKILL.md": (
                "---\nname: demo_skill_us\n"
                "description: 平台可挂载但 skills-ref 应拒绝的下划线名称。\n"
                "---\n\n# demo\n\n正文。\n"
            ).encode()
        }
    )


async def test_g0_runs_skills_ref(db: Database, seeded: Seed) -> None:
    p: People = await _people(db, seeded)
    raw_ok = _zip("demo-skill-ok", "合法 Skill：指导查询演示数据并汇总状态。")

    async with db.tx(DbScope.system()) as conn:
        aid = await registry.create_asset(
            conn, p.dev, type_="skill", name="skill.demo_ok", owner_org_id=seeded.hq, classification=2
        )
        vid = await registry.create_version(
            conn,
            p.dev,
            aid,
            "1.0.0",
            {"name": "demo-skill-ok", "description": "合法 Skill：指导查询演示数据并汇总状态。"},
        )
        await gates.set_acceptance(conn, p.dev, vid, {"scenarios": ["查询演示"]})
        await skill.attach_package(conn, p.dev, vid, raw_ok, key=KEY)
        g0 = await gates.submit(conn, p.dev, vid)
        assert g0["passed"] is True, g0

        aid2 = await registry.create_asset(
            conn, p.dev, type_="skill", name="skill.demo_us", owner_org_id=seeded.hq, classification=2
        )
        vid2 = await registry.create_version(
            conn,
            p.dev,
            aid2,
            "1.0.0",
            {"name": "demo_skill_us", "description": "平台可挂载但 skills-ref 应拒绝的下划线名称。"},
        )
        await gates.set_acceptance(conn, p.dev, vid2, {"scenarios": ["下划线名"]})
        await skill.attach_package(conn, p.dev, vid2, _zip_underscore(), key=KEY)
        g0b = await gates.submit(conn, p.dev, vid2)
        assert g0b["passed"] is False
        assert any("skills-ref" in p for p in g0b["problems"])
