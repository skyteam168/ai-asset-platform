"""H21：门禁 / 审批 / 使用申请 response_model。"""

from __future__ import annotations

from typing import Any, get_origin

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from aip.control_plane.app import create_app
from aip.control_plane.gates import schemas as gates_out
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


def test_gates_and_approval_response_models() -> None:
    app = create_app()
    expected: dict[str, Any] = {
        "addTestCase": gates_out.TestCase,
        "putSelectionEval": gates_out.SelectionEval,
        "getSelectionEval": gates_out.SelectionEval,
        "setAcceptance": gates_out.AcceptanceSheet,
        "submitVersion": gates_out.GateSubmitResult,
        "runTests": gates_out.LabRunResult,
        "gateHistory": Page[gates_out.GateResult],
        "listVersionImpact": gates_out.VersionImpact,
        "startCanary": gates_out.CanaryStarted,
        "promoteVersion": gates_out.PromoteResult,
        "listApprovals": Page[gates_out.ApprovalPending],
        "decideApproval": gates_out.ApprovalDecision,
        "addApprovalDelegate": gates_out.ApprovalDelegate,
        "requestUsage": gates_out.UsageRequestCreated,
        "openAccessReview": gates_out.AccessReviewOpened,
        "closeAccessReview": gates_out.AccessReviewClosed,
    }
    for op_id, model in expected.items():
        route = _route(app, op_id)
        rm = route.response_model
        assert rm is not None and rm is not dict, op_id
        if get_origin(model) is Page:
            assert get_origin(rm) is Page, op_id
        else:
            assert isinstance(rm, type) and issubclass(rm, BaseModel) and rm is model, op_id
