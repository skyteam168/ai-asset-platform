"""H15：控制面 auth/IAM 核心路由挂载 Pydantic response_model。"""

from __future__ import annotations

from typing import Any, get_origin

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from aip.control_plane.app import create_app
from aip.control_plane.iam import schemas as iam_out
from aip.libs.common.paging import Page


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


def _is_model(model: Any) -> bool:
    if model is None or model is dict:
        return False
    origin = get_origin(model)
    if origin is not None:
        return origin is Page or (isinstance(origin, type) and issubclass(origin, BaseModel))
    return isinstance(model, type) and issubclass(model, BaseModel)


def test_auth_iam_core_have_response_models() -> None:
    app = create_app()
    expected: dict[str, Any] = {
        "login": iam_out.Session,
        "me": iam_out.Me,
        "listOrgs": Page[iam_out.OrgUnit],
        "createOrg": iam_out.OrgUnit,
        "getOrg": iam_out.OrgUnit,
        "moveOrg": iam_out.OrgUnit,
        "disableOrg": iam_out.OrgUnit,
        "listUsers": Page[iam_out.User],
        "createUser": iam_out.User,
        "getUser": iam_out.User,
        "importUsers": iam_out.ImportResult,
        "rollbackImport": iam_out.ImportRollback,
        "assignRole": iam_out.RoleAssignment,
    }
    for op_id, model in expected.items():
        route = _route(app, op_id)
        assert _is_model(route.response_model), f"{op_id} response_model={route.response_model!r}"
        # 泛型 Page[...] 在运行时可能是 typing._GenericAlias，只校验“同类”
        if get_origin(model) is Page:
            assert get_origin(route.response_model) is Page, op_id
        else:
            assert route.response_model is model, op_id
