"""Connector worker 池（architecture.md 9.5 / H57 / G5.1）。

默认 `inline`：与 Runtime 同进程。`rpc`：Runtime 经内部 HTTP JSON 调用独立 worker；
出站必须经 `libs/httpclient.build_client`。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from aip.libs.common.config import Settings, get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient import build_client
from aip.libs.httpclient.config import ConnectionConfig
from aip.libs.httpclient.factory import map_error

log = logging.getLogger("aip.connector_worker")

EXECUTE_PATH = "/v1/connector-worker/execute"


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


def job_from_dict(raw: dict[str, Any]) -> WorkerJob:
    return WorkerJob(
        connector_kind=str(raw.get("connector_kind") or ""),
        op=str(raw.get("op") or ""),
        params=dict(raw.get("params") or {}),
        connection_id=str(raw.get("connection_id") or ""),
        idempotency_key=str(raw.get("idempotency_key") or ""),
    )


def result_to_dict(result: WorkerResult) -> dict[str, Any]:
    return {"ok": result.ok, "data": result.data, "error": result.error}


def result_from_dict(raw: dict[str, Any]) -> WorkerResult:
    return WorkerResult(ok=bool(raw.get("ok")), data=raw.get("data"), error=raw.get("error"))


class RpcConnectorWorker:
    """独立池：POST {base}/v1/connector-worker/execute。"""

    mode = "rpc"

    def __init__(self, base_url: str, *, connect_timeout_s: float = 3.0, read_timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s

    def _connection(self) -> ConnectionConfig:
        if not self.base_url:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "connector_worker_url 未配置")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "connector_worker_url 非法")
        scheme = "https" if parsed.scheme == "https" else "http"
        port = parsed.port or (443 if scheme == "https" else 80)
        base = f"{scheme}://{parsed.hostname}:{port}"
        return ConnectionConfig(
            id="connector-worker",
            base_url=base,
            scheme=scheme,  # type: ignore[arg-type]
            tls_mode="system_ca" if scheme == "https" else None,
            transport_exception_reason="internal connector worker" if scheme == "http" else None,
            transport_exception_expires_at=(
                dt.datetime(2099, 1, 1, tzinfo=dt.UTC) if scheme == "http" else None
            ),
            connect_timeout_s=self.connect_timeout_s,
            read_timeout_s=self.read_timeout_s,
        )

    async def execute(self, job: WorkerJob) -> WorkerResult:
        cfg = self._connection()
        try:
            async with build_client(cfg, trace_id="connector-worker") as client:
                r = await client.post(EXECUTE_PATH, json=asdict(job))
        except PlatformError:
            raise
        except Exception as e:
            raise map_error(e) from e
        if r.status_code >= 500:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "connector worker 5xx")
        if r.status_code == 404:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "connector worker 路由不存在")
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail") or body.get("message") or "")[:200]
            except Exception:
                detail = ""
            raise PlatformError(
                ErrorCode.DOWNSTREAM_UNAVAILABLE,
                f"connector worker 拒绝：HTTP {r.status_code}" + (f" {detail}" if detail else ""),
            )
        try:
            raw = r.json()
        except Exception as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "connector worker 返回非 JSON") from e
        if not isinstance(raw, dict):
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "connector worker 返回非对象")
        return result_from_dict(raw)


def build_connector_worker(settings: Settings | None = None) -> ConnectorWorker:
    s = settings or get_settings()
    if s.connector_worker_mode == "rpc":
        return RpcConnectorWorker(s.connector_worker_url)
    return InlineConnectorWorker()
