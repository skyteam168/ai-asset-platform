"""Connector worker HTTP 服务：接收 Runtime 内部 RPC（G5.1）。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from aip.libs.connector_worker import (
    InlineConnectorWorker,
    WorkerResult,
    job_from_dict,
    result_to_dict,
)


class ExecuteIn(BaseModel):
    connector_kind: str = ""
    op: str
    params: dict[str, Any] = Field(default_factory=dict)
    connection_id: str = ""
    idempotency_key: str = ""


def create_connector_worker_app(*, handler: Any | None = None) -> FastAPI:
    """handler 可选：async (WorkerJob) -> WorkerResult | Any；默认 echo。"""
    worker = InlineConnectorWorker(handler=handler)
    app = FastAPI(title="AIP Connector Worker", version="0.1.0")
    app.state.worker = worker

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/connector-worker/execute")
    async def execute(body: ExecuteIn) -> dict[str, Any]:
        job = job_from_dict(body.model_dump())
        result: WorkerResult = await worker.execute(job)
        return result_to_dict(result)

    return app
