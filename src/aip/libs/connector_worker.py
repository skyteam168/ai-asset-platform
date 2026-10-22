"""Connector worker 池骨架（architecture.md 9.5 / H57）。

首期 Runtime 与 Connector 同进程（inline）。阻塞 SDK / RPA 类连接器部署到独立
worker 池时，经本接口 RPC；本片仅登记协议与 inline 实现，不引入消息队列。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError


@dataclass(frozen=True)
class WorkerJob:
    connector_kind: str
    op: str
    params: dict[str, Any]
    connection_id: str
    idempotency_key: str


@dataclass(frozen=True)
class WorkerResult:
    ok: bool
    data: Any = None
    error: str | None = None


class ConnectorWorker(Protocol):
    mode: str

    async def execute(self, job: WorkerJob) -> WorkerResult: ...


class InlineConnectorWorker:
    """同进程执行：把调用交回 Runtime 侧已注入的 handler（测例用）。"""

    mode = "inline"

    def __init__(self, handler: Any | None = None) -> None:
        self._handler = handler

    async def execute(self, job: WorkerJob) -> WorkerResult:
        if self._handler is None:
            return WorkerResult(ok=True, data={"echo": job.op, "mode": self.mode})
        out = await self._handler(job)
        if isinstance(out, WorkerResult):
            return out
        return WorkerResult(ok=True, data=out)


class RpcConnectorWorker:
    """独立池骨架：要求配置 connector_worker_url；本片不发起真实 RPC。"""

    mode = "rpc"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def execute(self, job: WorkerJob) -> WorkerResult:
        if not self.base_url:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "connector_worker_url 未配置")
        raise PlatformError(
            ErrorCode.PRECONDITION_FAILED,
            f"RPC connector worker 尚未接线（target={self.base_url}）；本地用 AIP_CONNECTOR_WORKER_MODE=inline",
        )


def build_connector_worker(settings: Settings | None = None) -> ConnectorWorker:
    s = settings or get_settings()
    if s.connector_worker_mode == "rpc":
        return RpcConnectorWorker(s.connector_worker_url)
    return InlineConnectorWorker()
