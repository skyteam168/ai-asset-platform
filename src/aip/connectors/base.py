"""Connector SDK 契约（architecture.md 9.8）。Connector 不能自己创建 HTTP 客户端，只能用 ctx.http()。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from aip.libs.common.errors import ErrorCode, PlatformError

Mode = Literal["sandbox", "live_read", "live", "replay"]


@dataclass
class ExecContext:
    trace_id: str
    deadline: float  # time.monotonic() 截止时刻
    mode: Mode
    idempotency_key: str | None = None
    http: Callable[[], httpx.AsyncClient] | None = None
    connection_env: str | None = None
    external_codes: dict[str, dict[str, str]] = field(default_factory=dict)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def require_time(self) -> float:
        left = self.remaining()
        if left <= 0:
            raise PlatformError(ErrorCode.DOWNSTREAM_TIMEOUT, "超过调用总期限")
        return left

    def guard_write(self) -> None:
        """沙箱模式下 live 写操作在 Connector 层硬拒绝（architecture.md 7.2、9.7）。"""
        if self.mode == "sandbox" and self.connection_env not in ("sandbox", "test", None):
            raise PlatformError(ErrorCode.SANDBOX_WRITE_BLOCKED, "沙箱模式禁止写入非测试环境")
        if self.mode in ("live_read", "replay"):
            raise PlatformError(ErrorCode.SANDBOX_WRITE_BLOCKED, f"{self.mode} 模式禁止写操作")


@dataclass
class ConnectorResult:
    data: Any
    next_cursor: str | None = None
    status_code: int | None = None


@dataclass
class ReconcileResult:
    outcome: Literal["succeeded", "not_executed", "unknown"]
    data: Any = None


class Connector(Protocol):
    name: str

    def capabilities(self) -> list[str]: ...

    def is_write(self, op: str) -> bool: ...

    async def execute(self, op: str, params: dict[str, Any], ctx: ExecContext) -> ConnectorResult: ...

    async def health(self, ctx: ExecContext) -> bool: ...

    async def reconcile(self, op: str, idempotency_key: str, ctx: ExecContext) -> ReconcileResult: ...


def http_status_error(status: int) -> PlatformError | None:
    """下游 HTTP 状态 → 平台错误（业务码失败由 classify 处理）。"""
    if status < 400:
        return None
    code = {
        400: ErrorCode.INVALID_ARGUMENT,
        401: ErrorCode.DOWNSTREAM_AUTH_EXPIRED,
        403: ErrorCode.PERMISSION_DENIED,
        404: ErrorCode.BUSINESS_RULE_VIOLATION,
        409: ErrorCode.BUSINESS_RULE_VIOLATION,
        422: ErrorCode.BUSINESS_RULE_VIOLATION,
        429: ErrorCode.RATE_LIMITED,
    }.get(status)
    if code is None:
        code = ErrorCode.DOWNSTREAM_UNAVAILABLE if status >= 500 else ErrorCode.BUSINESS_RULE_VIOLATION
    return PlatformError(code, f"下游返回 HTTP {status}")
