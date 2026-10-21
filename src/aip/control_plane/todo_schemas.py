"""平台待办与 Run 控制台视图响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TodoConfirmation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    company_id: str | None = None
    kind: str | None = None
    capability_id: str | None = None
    version: str | None = None
    consumer_id: str | None = None
    end_user_id: str | None = None
    summary: str | None = None
    approver_role: str | None = None
    status: str | None = None
    expires_at: str | None = None
    decided_by: str | None = None
    decided_via: str | None = None
    decided_at: str | None = None
    execution_id: str | None = None
    run_id: str | None = None
    args_sha256: str | None = None
    created_at: str | None = None


class TodoTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    kind: str | None = None
    summary: str | None = None
    company_id: str | None = None
    assignee_user_id: str | None = None
    assignee_role: str | None = None
    assignee_org_id: str | None = None
    deadline: str | None = None
    status: str | None = None
    run_id: str | None = None


class TodoList(BaseModel):
    confirmations: list[TodoConfirmation] = Field(default_factory=list)
    tasks: list[TodoTask] = Field(default_factory=list)


class TodoDecideResult(BaseModel):
    """确认 / 人工任务决定结果（字段随数据面实现变化）。"""

    model_config = ConfigDict(extra="allow")

    status: str | None = None
    id: str | None = None


class RunConsoleView(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str | None = None
    company_id: str | None = None
    status: str | None = None
    result: Any = None
    error: Any = None
    tainted: bool | None = None
    pending_tasks: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    trace_id: str | None = None
