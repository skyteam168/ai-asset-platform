"""H20：控制台读路径与连接 response_model。"""

from __future__ import annotations

from typing import Any, get_origin

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from aip.control_plane import console_schemas as console_out
from aip.control_plane.app import create_app
from aip.control_plane.conn import schemas as conn_out
from aip.control_plane.registry.schemas import GrantCeiling
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


def test_console_and_connection_response_models() -> None:
    app = create_app()
    expected: dict[str, Any] = {
        "listConnections": Page[conn_out.SystemConnection],
        "createConnection": conn_out.SystemConnection,
        "approveTransportException": conn_out.TransportExceptionStatus,
        "testConnection": conn_out.ConnectionTestResult,
        "draftTrust": conn_out.ConfigDraft,
        "listVersions": Page[console_out.VersionSummary],
        "getVersion": console_out.VersionDetail,
        "listCeilings": Page[GrantCeiling],
        "listUsageRequests": Page[console_out.UsageRequest],
        "searchAudit": Page[console_out.ConsoleAuditRecord],
        "usageReport": console_out.UsageReport,
        "verifyAudit": console_out.AuditVerifyResult,
        "consoleCatalog": console_out.ConsoleCatalog,
    }
    for op_id, model in expected.items():
        route = _route(app, op_id)
        rm = route.response_model
        assert rm is not None and rm is not dict, op_id
        if get_origin(model) is Page:
            assert get_origin(rm) is Page, op_id
        else:
            assert isinstance(rm, type) and issubclass(rm, BaseModel) and rm is model, op_id
