"""H19：IAM 消费方/业务角色与 Registry 资产/授权 response_model。"""

from __future__ import annotations

from typing import Any, get_origin

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from aip.control_plane.app import create_app
from aip.control_plane.iam import schemas as iam_out
from aip.control_plane.registry import schemas as reg_out
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


def test_iam_consumer_and_registry_response_models() -> None:
    app = create_app()
    expected: dict[str, Any] = {
        "listBusinessRoles": Page[iam_out.BusinessRole],
        "createBusinessRole": iam_out.BusinessRole,
        "requestClearance": iam_out.ClearanceRequest,
        "decideClearance": iam_out.ClearanceDecision,
        "listConsumers": Page[iam_out.Consumer],
        "createConsumer": iam_out.Consumer,
        "issueCredential": iam_out.CredentialIssued,
        "listBindings": Page[iam_out.UserBinding],
        "createBinding": iam_out.UserBinding,
        "debugAssertion": iam_out.AssertionDebugResult,
        "listAssets": Page[reg_out.Asset],
        "createAsset": reg_out.Asset,
        "createVersion": reg_out.AssetVersion,
        "listGrants": Page[reg_out.Grant],
        "createGrant": reg_out.Grant,
        "createCeiling": reg_out.GrantCeiling,
    }
    for op_id, model in expected.items():
        route = _route(app, op_id)
        rm = route.response_model
        assert rm is not None and rm is not dict, op_id
        if get_origin(model) is Page:
            assert get_origin(rm) is Page, op_id
        else:
            assert isinstance(rm, type) and issubclass(rm, BaseModel), op_id
            assert rm is model, op_id
