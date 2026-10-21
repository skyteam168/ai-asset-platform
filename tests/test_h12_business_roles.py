"""H12：公司业务角色登记、分配与 Workflow assignee。"""

from __future__ import annotations

import pytest

from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam import business_roles, users
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.workflow.dsl import validate
from iam_seed import Seed
from test_m4_gates import _people


@pytest.mark.asyncio
async def test_create_assign_and_dsl(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        role = await business_roles.create(
            conn, p.admin, seeded.vn, "purchase_manager", "采购经理", "审批采购单"
        )
        assert role["code"] == "purchase_manager"
        rid = await users.assign_role(
            conn, p.admin, seeded.users["vn_admin"], "purchase_manager", seeded.vn
        )
        assert rid
        actor = await users.load_actor(conn, seeded.users["vn_admin"])
        assert actor.has("purchase_manager")

    problems = validate(
        {
            "name": "po",
            "capability_id": "agent.finance.po",
            "version": "1.0.0",
            "description": "采购审批",
            "budget": {"max_steps": 10, "max_llm_calls": 0, "timeout_s": 60},
            "input_schema": {"type": "object", "properties": {"company": {"type": "string"}}},
            "nodes": [
                {
                    "id": "h",
                    "type": "human_task",
                    "kind": "approve",
                    "assignee": "role:purchase_manager@org_of(input.company)",
                    "next": "done",
                },
                {"id": "done", "type": "end", "output": {}},
            ],
        }
    )
    assert problems == [], problems


@pytest.mark.asyncio
async def test_reject_platform_code_as_business(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError) as e:
            await business_roles.create(conn, p.admin, seeded.vn, "platform_admin", "x")
        assert e.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_assign_unknown_business_rejected(db: Database, seeded: Seed) -> None:
    p = await _people(db, seeded)
    async with db.tx(DbScope.system()) as conn:
        with pytest.raises(PlatformError, match="未知角色"):
            await users.assign_role(conn, p.admin, seeded.users["vn_admin"], "no_such_role", seeded.vn)


def test_dsl_schema_rejects_invalid_role_token() -> None:
    problems = validate(
        {
            "name": "x",
            "capability_id": "agent.demo.flow",
            "version": "1.0.0",
            "description": "x",
            "budget": {"max_steps": 5, "max_llm_calls": 0, "timeout_s": 30},
            "input_schema": {"type": "object"},
            "nodes": [
                {
                    "id": "h",
                    "type": "human_task",
                    "kind": "approve",
                    "assignee": "role:Bad-Role@VN01",
                    "next": "done",
                },
                {"id": "done", "type": "end", "output": {}},
            ],
        }
    )
    assert any("assignee" in p for p in problems)
