"""两个 FastAPI 应用共用的装配：Problem Details 错误、trace id、健康检查模型。"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from aip.libs.common.errors import ErrorCode, PlatformError

log = logging.getLogger("aip")
PROBLEM = "application/problem+json"


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    checks: dict[str, str] = {}


def _trace_id(request: Request) -> str:
    tp = request.headers.get("traceparent", "")
    parts = tp.split("-")
    if len(parts) == 4 and len(parts[1]) == 32:
        return parts[1]
    return secrets.token_hex(16)


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def trace(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request.state.trace_id = _trace_id(request)
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(
            exc.problem(request.url.path, request.state.trace_id), status_code=exc.status, media_type=PROBLEM
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: ErrorCode.NOT_FOUND,
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.PERMISSION_DENIED,
            405: ErrorCode.INVALID_ARGUMENT,
        }.get(exc.status_code, ErrorCode.INTERNAL)
        body = PlatformError(code, str(exc.detail)).problem(request.url.path, request.state.trace_id)
        body["status"] = exc.status_code
        return JSONResponse(body, status_code=exc.status_code, media_type=PROBLEM)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"]), "code": "INVALID_ARGUMENT", "message": e["msg"]}
            for e in exc.errors()
        ]
        err = PlatformError(ErrorCode.INVALID_ARGUMENT, "请求参数不合法", errors)
        return JSONResponse(err.problem(request.url.path, request.state.trace_id), status_code=400, media_type=PROBLEM)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error trace_id=%s", getattr(request.state, "trace_id", ""))
        err = PlatformError(ErrorCode.INTERNAL, "平台内部错误")
        return JSONResponse(
            err.problem(request.url.path, getattr(request.state, "trace_id", "")), status_code=500, media_type=PROBLEM
        )
