"""确认通道（architecture.md 4.5）与平台待办的数据面内部接口。

- 消费方回调：POST /api/v1/confirmations/{id}/confirm，必须携带同一用户的新断言
  （jti 未用过、iat 不早于 confirmation 创建时间），user_id 一致才执行；应用身份不能确认 user_confirm。
- 平台待办：控制台在控制面完成登录与授权判断后，经服务凭据调用 /api/v1/internal/*；数据面只信任服务凭据。
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel

from aip.data_plane.confirmation import public_view
from aip.data_plane.oauth import AuthContext
from aip.data_plane.runtime import ToolRuntime
from aip.libs.common.errors import ErrorCode, PlatformError

V = "/api/v1"


class ConfirmIn(BaseModel):
    assertion: str | None = None
    comment: str | None = None
    args: dict[str, Any] | None = None  # 可选：消费方声明自己确认的参数，必须与冻结内容一致


class DecideIn(BaseModel):
    decision: Literal["approve", "reject"]
    user_id: str
    comment: str | None = None
    data: dict[str, Any] | None = None


class TodoQuery(BaseModel):
    company_ids: list[str] | None = None  # None = 全部公司（平台管理员、审计员）


def require_service(authorization: str | None, token: str) -> None:
    given = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not secrets.compare_digest(given, token):
        raise PlatformError(ErrorCode.UNAUTHENTICATED, "需要服务凭据")


def _created(row: dict[str, Any]) -> dt.datetime:
    c = row["created_at"]
    return c if isinstance(c, dt.datetime) else dt.datetime.fromisoformat(c)


def router(ctx: AuthContext, runtime: ToolRuntime, workflows: Any = None) -> APIRouter:
    r = APIRouter()
    service_token = ctx.settings.internal_service_token

    async def own(cid: str, authorization: str | None) -> tuple[Any, dict[str, Any]]:
        caller = await ctx.principal_from_request(authorization, None)
        row = await runtime.confirmations.get(cid)
        if row is None or str(row["consumer_id"]) != caller.consumer_id:
            raise PlatformError(ErrorCode.NOT_FOUND, "确认不存在")
        return caller, row

    @r.post(f"{V}/confirmations/{{id}}/confirm", tags=["confirmation"], operation_id="confirm")
    async def confirm(
        id: str, body: ConfirmIn, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        caller, row = await own(id, authorization)
        if row["kind"] != "user_confirm":
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "审批类确认只能由审批人在平台待办处理")
        if not body.assertion:
            raise PlatformError(ErrorCode.ASSERTION_INVALID, "需要被代表用户的新断言")
        _, consumer = ctx.resolver.consumer(caller.client_id)
        v = await ctx.verify_user_assertion(body.assertion, caller.client_id, consumer, ctx.audiences)
        # iat 是整秒：允许与创建时间同一秒，但不接受确认对象创建之前签发的断言
        if v.issued_at < _created(row).replace(microsecond=0):
            raise PlatformError(ErrorCode.ASSERTION_INVALID, "断言签发时间早于确认对象创建时间")
        user = ctx.resolver.for_assertion(v, "header_assertion")
        if user.user_id is None or user.user_id != str(row["end_user_id"]):
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "必须由被代表的同一用户确认")
        return await runtime.execute_confirmation(
            id, by=f"user:{user.user_id}", via="callback", comment=body.comment, expect_args=body.args
        )

    @r.post(f"{V}/confirmations/{{id}}/reject", tags=["confirmation"], operation_id="rejectConfirmation")
    async def reject(id: str, body: ConfirmIn, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        caller, _ = await own(id, authorization)
        return await runtime.reject_confirmation(
            id, by=f"consumer:{caller.client_id}", via="callback", comment=body.comment
        )

    # ---------------- 平台待办（控制面经服务凭据调用） ----------------

    @r.get(f"{V}/internal/confirmations/{{id}}", tags=["internal"], operation_id="internalGetConfirmation")
    async def get_confirmation(id: str, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        require_service(authorization, service_token)
        row = await runtime.confirmations.get(id)
        if row is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "确认不存在")
        return public_view(row)

    @r.post(f"{V}/internal/confirmations/{{id}}/decide", tags=["internal"], operation_id="internalDecideConfirmation")
    async def decide_confirmation(
        id: str, body: DecideIn, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        """调用前控制面已核对：approval 由具备 approver_role 的人处理且不是被代表用户本人；
        user_confirm 只能由被代表用户本人在控制台确认。"""
        require_service(authorization, service_token)
        if body.decision == "approve":
            return await runtime.execute_confirmation(
                id, by=f"user:{body.user_id}", via="console", comment=body.comment
            )
        return await runtime.reject_confirmation(id, by=f"user:{body.user_id}", via="console", comment=body.comment)

    @r.post(f"{V}/internal/todo", tags=["internal"], operation_id="internalTodo")
    async def todo(body: TodoQuery, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        require_service(authorization, service_token)
        rows = await runtime.confirmations.pending(body.company_ids)
        tasks = await workflows.pending_tasks(body.company_ids) if workflows is not None else []
        return {"confirmations": [public_view(x) for x in rows], "tasks": tasks}

    def need_engine() -> Any:
        if workflows is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "本数据面未启用 Workflow 引擎")
        return workflows

    @r.get(f"{V}/internal/tasks/{{id}}", tags=["internal"], operation_id="internalGetTask")
    async def get_task(id: str, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        require_service(authorization, service_token)
        t = await need_engine().task(id)
        if t is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "任务不存在")
        return dict(t)

    @r.post(f"{V}/internal/tasks/{{id}}/decide", tags=["internal"], operation_id="internalDecideTask")
    async def decide_task(
        id: str, body: DecideIn, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        require_service(authorization, service_token)
        result: dict[str, Any] = await need_engine().decide_task(
            id, decision=body.decision, user_id=body.user_id, data=body.data, comment=body.comment
        )
        return result

    @r.get(f"{V}/internal/runs/{{id}}", tags=["internal"], operation_id="internalGetRun")
    async def get_run(id: str, authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        require_service(authorization, service_token)
        view: dict[str, Any] = await need_engine().view(id)
        return view

    return r
