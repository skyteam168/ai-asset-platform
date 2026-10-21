"""平台待办（architecture.md 4.5 第 3 种确认通道、8.5 human_task）。

确认对象与人工任务存在数据面运行库；控制台登录、授权判断在控制面完成，再经服务凭据调用数据面内部接口。
授权规则：
- approval 类确认：具备 approver_role 且角色生效范围覆盖该公司；不能是被代表用户本人（职责分离）。
- user_confirm：只能由被代表用户本人确认。
- 人工任务：指定了人则只能是那个人；否则需具备 assignee_role 且生效范围覆盖任务所在组织。
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane import todo_schemas as out
from aip.control_plane.approval import service as approval
from aip.control_plane.db import Database, DbScope
from aip.control_plane.iam.api import ActorDep
from aip.control_plane.iam.authz import Actor
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError

V = "/api/v1"


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = None
    data: dict[str, Any] | None = None


class DataPlane:
    """数据面内部接口客户端；测试可注入 ASGI transport。"""

    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.http = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"}, transport=transport, timeout=30
        )

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            r = await self.http.request(method, f"{V}/internal{path}", json=body)
        except httpx.HTTPError as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "数据面不可达") from e
        data = r.json() if r.content else None
        if r.status_code >= 400:
            code = (data or {}).get("code", "INTERNAL")
            raise PlatformError(
                ErrorCode(code) if code in ErrorCode.__members__ else ErrorCode.INTERNAL,
                (data or {}).get("detail", "数据面返回错误"),
            )
        return data


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def qualifies(actor: Actor, role: str | None, org_id: Any) -> bool:
    org = _uuid(org_id)
    if not role or org is None or actor.system:
        return False
    return any(g.role == role and org in actor.subtrees.get(g.scope_org_id, frozenset()) for g in actor.roles)


def can_confirm(actor: Actor, c: dict[str, Any]) -> bool:
    me = str(actor.user_id) if actor.user_id else None
    if c["kind"] == "user_confirm":
        return me is not None and me == str(c.get("end_user_id"))
    return qualifies(actor, c.get("approver_role"), c["company_id"]) and me != str(c.get("end_user_id"))


def can_handle_task(actor: Actor, t: dict[str, Any]) -> bool:
    if t.get("assignee_user_id"):
        return actor.user_id is not None and str(actor.user_id) == str(t["assignee_user_id"])
    return qualifies(actor, t.get("assignee_role"), t.get("assignee_org_id"))


async def can_handle_task_with_delegate(conn: AsyncConnection, actor: Actor, t: dict[str, Any]) -> bool:
    """指定处理人时，有效代理人也可处理（与审批代理人同一张表）。"""
    if can_handle_task(actor, t):
        return True
    if not t.get("assignee_user_id") or actor.user_id is None:
        return False
    try:
        assignee = uuid.UUID(str(t["assignee_user_id"]))
    except ValueError:
        return False
    principals = await approval._active_principals(conn, actor)
    return assignee in principals


def router() -> APIRouter:
    r = APIRouter()

    def dp(request: Request) -> DataPlane:
        client: DataPlane | None = getattr(request.app.state, "data_plane", None)
        if client is None:
            s = get_settings()
            client = request.app.state.data_plane = DataPlane(s.data_plane_url, s.internal_service_token)
        return client

    def db(request: Request) -> Database:
        database: Database = request.app.state.db
        return database

    @r.get(f"{V}/todo", tags=["approval"], operation_id="listTodo", response_model=out.TodoList)
    async def todo(request: Request, actor: ActorDep) -> out.TodoList:
        """我的待办：可处理的确认（审批 / 本人确认）与 Workflow 人工任务。"""
        companies = None if actor.is_global else sorted({str(g.scope_company_id) for g in actor.roles})
        data = await dp(request).call("POST", "/todo", {"company_ids": companies})
        confirmations = [c for c in data["confirmations"] if can_confirm(actor, c)]
        # user_confirm 的被代表用户不一定有角色：再按本人查一次（公司过滤为空时也能看到自己的确认）
        if companies is not None and actor.company_id and str(actor.company_id) not in companies:
            mine = await dp(request).call("POST", "/todo", {"company_ids": [str(actor.company_id)]})
            confirmations += [c for c in mine["confirmations"] if can_confirm(actor, c)]
        async with db(request).tx(DbScope.system()) as conn:
            tasks = [t for t in data["tasks"] if await can_handle_task_with_delegate(conn, actor, t)]
        return out.TodoList.model_validate({"confirmations": confirmations, "tasks": tasks})

    @r.post(
        f"{V}/todo/confirmations/{{id}}/decide",
        tags=["approval"],
        operation_id="decideConfirmation",
        response_model=out.TodoDecideResult,
    )
    async def decide_confirmation(
        id: uuid.UUID, body: DecisionIn, request: Request, actor: ActorDep
    ) -> out.TodoDecideResult:
        c = await dp(request).call("GET", f"/confirmations/{id}")
        actor.require(can_confirm(actor, c), "无权处理该确认（需具备审批角色，且不能确认自己发起的调用）")
        data = await dp(request).call(
            "POST",
            f"/confirmations/{id}/decide",
            {"decision": body.decision, "user_id": str(actor.user_id), "comment": body.comment},
        )
        return out.TodoDecideResult.model_validate(data)

    @r.post(
        f"{V}/todo/tasks/{{id}}/decide",
        tags=["approval"],
        operation_id="decideTask",
        response_model=out.TodoDecideResult,
    )
    async def decide_task(id: uuid.UUID, body: DecisionIn, request: Request, actor: ActorDep) -> out.TodoDecideResult:
        t = await dp(request).call("GET", f"/tasks/{id}")
        async with db(request).tx(DbScope.system()) as conn:
            ok = await can_handle_task_with_delegate(conn, actor, t)
        actor.require(ok, "无权处理该任务")
        on_behalf = None
        if (
            t.get("assignee_user_id")
            and actor.user_id is not None
            and str(actor.user_id) != str(t["assignee_user_id"])
        ):
            on_behalf = str(t["assignee_user_id"])
        data = await dp(request).call(
            "POST",
            f"/tasks/{id}/decide",
            {
                "decision": body.decision,
                "user_id": str(actor.user_id),
                "comment": body.comment,
                "data": body.data,
                "on_behalf_of": on_behalf,
            },
        )
        return out.TodoDecideResult.model_validate(data)

    @r.get(
        f"{V}/runs/{{id}}",
        tags=["approval"],
        operation_id="getRunConsole",
        response_model=out.RunConsoleView,
    )
    async def run_view(id: uuid.UUID, request: Request, actor: ActorDep) -> out.RunConsoleView:
        view = await dp(request).call("GET", f"/runs/{id}")
        if not actor.is_global:
            companies = {str(g.scope_company_id) for g in actor.roles}
            async with db(request).tx(DbScope.system()) as conn:
                task_ok = False
                for t in view.get("pending_tasks", []):
                    if await can_handle_task_with_delegate(conn, actor, t):
                        task_ok = True
                        break
            actor.require(task_ok or view["company_id"] in companies, "无权查看该 Run")
        return out.RunConsoleView.model_validate(view)

    return r
