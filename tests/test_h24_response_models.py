"""H24：Importer response_model。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from aip.control_plane.app import create_app
from aip.control_plane.importer import schemas as importer_out


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


def test_importer_response_models() -> None:
    app = create_app()
    for op_id in ("importOpenApi", "importPostman"):
        route = _route(app, op_id)
        rm = route.response_model
        assert isinstance(rm, type) and issubclass(rm, BaseModel)
        assert rm is importer_out.ImporterResult
