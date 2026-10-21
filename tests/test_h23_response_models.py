"""H23：todo / LLM response_model。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from aip.control_plane import todo_schemas as todo_out
from aip.control_plane.app import create_app
from aip.control_plane.llm import schemas as llm_out


def _flatten(routes: list[Any]) -> list[Any]:
    out: list[Any] = []
    for r in routes:
        inner = getattr(r, "original_router", None)
        out += _flatten(list(inner.routes)) if inner is not None else [r]
    return out


def _route(app: FastAPI, op_id: str) -> APIRoute:
    for r in _flatten(list(app.routes)):
        if isinstance(r, APIRoute) and r.operation_id == op_id:
            return r
    raise AssertionError(f"missing route {op_id}")


def test_todo_and_llm_response_models() -> None:
    app = create_app()
    expected: dict[str, Any] = {
        "listTodo": todo_out.TodoList,
        "decideConfirmation": todo_out.TodoDecideResult,
        "decideTask": todo_out.TodoDecideResult,
        "getRunConsole": todo_out.RunConsoleView,
        "getLlmAllowlist": llm_out.LlmAllowlist,
        "putLlmAllowlist": llm_out.LlmAllowlist,
        "getLlmBudget": llm_out.LlmBudget,
        "putLlmBudget": llm_out.LlmBudget,
        "getLlmUsage": llm_out.LlmUsage,
    }
    for op_id, model in expected.items():
        route = _route(app, op_id)
        rm = route.response_model
        assert rm is not None and rm is not dict, op_id
        assert isinstance(rm, type) and issubclass(rm, BaseModel) and rm is model, op_id
