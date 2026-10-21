"""公司 LLM 白名单与日 token 预算 API。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aip.control_plane.iam.api import ActorDep, DbDep
from aip.control_plane.iam.orgs import get_org
from aip.control_plane.llm import schemas as out
from aip.data_plane.llm import allowlist as al
from aip.data_plane.llm import budget as bd
from aip.libs.common.errors import ErrorCode, PlatformError

V = "/api/v1"


def _company(org: Any) -> None:
    if org.type != "company":
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "id 须为公司")


class AllowlistIn(BaseModel):
    names: list[str] = Field(default_factory=list)


class BudgetIn(BaseModel):
    daily_tokens: int = Field(ge=0)


def router() -> APIRouter:
    r = APIRouter(tags=["llm"])

    @r.get(
        f"{V}/companies/{{id}}/llm-allowlist",
        operation_id="getLlmAllowlist",
        response_model=out.LlmAllowlist,
    )
    async def get_allowlist(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.LlmAllowlist:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看 LLM 白名单")
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            names = await al.list_names_on_conn(conn, id)
        return out.LlmAllowlist(company_id=str(id), names=names)

    @r.put(
        f"{V}/companies/{{id}}/llm-allowlist",
        operation_id="putLlmAllowlist",
        response_model=out.LlmAllowlist,
    )
    async def put_allowlist(id: uuid.UUID, body: AllowlistIn, actor: ActorDep, db: DbDep) -> out.LlmAllowlist:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以配置 LLM 白名单")
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            await al.set_names_on_conn(conn, id, body.names)
            names = await al.list_names_on_conn(conn, id)
        return out.LlmAllowlist(company_id=str(id), names=names)

    @r.get(
        f"{V}/companies/{{id}}/llm-budget",
        operation_id="getLlmBudget",
        response_model=out.LlmBudget,
    )
    async def get_budget(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.LlmBudget:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看 LLM 预算")
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            daily = await bd.get_budget_on_conn(conn, id)
        return out.LlmBudget(company_id=str(id), daily_tokens=daily)

    @r.put(
        f"{V}/companies/{{id}}/llm-budget",
        operation_id="putLlmBudget",
        response_model=out.LlmBudget,
    )
    async def put_budget(id: uuid.UUID, body: BudgetIn, actor: ActorDep, db: DbDep) -> out.LlmBudget:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以配置 LLM 预算")
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            await bd.set_budget_on_conn(conn, id, body.daily_tokens)
        return out.LlmBudget(company_id=str(id), daily_tokens=body.daily_tokens)

    @r.get(
        f"{V}/companies/{{id}}/llm-usage",
        operation_id="getLlmUsage",
        response_model=out.LlmUsage,
    )
    async def get_usage(id: uuid.UUID, actor: ActorDep, db: DbDep) -> out.LlmUsage:
        actor.require(actor.has("platform_admin"), "只有平台管理员可以查看 LLM 用量")
        day = dt.datetime.now(dt.UTC).date()
        async with db.tx(actor.db_scope()) as conn:
            _company(await get_org(conn, id))
            used = await bd.get_usage_on_conn(conn, id, day)
            daily = await bd.get_budget_on_conn(conn, id)
        return out.LlmUsage(
            company_id=str(id),
            usage_date=day.isoformat(),
            tokens_used=used,
            daily_tokens=daily,
        )

    return r
