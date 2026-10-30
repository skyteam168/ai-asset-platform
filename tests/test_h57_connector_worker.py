"""H57 / G5.1：Connector worker inline 与进程间 RPC。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.connector_worker import (
    InlineConnectorWorker,
    RpcConnectorWorker,
    WorkerJob,
    build_connector_worker,
)
from aip.workers.connector_app import create_connector_worker_app


@pytest.mark.asyncio
async def test_inline_echo() -> None:
    w = InlineConnectorWorker()
    r = await w.execute(
        WorkerJob(
            connector_kind="rest",
            op="ping",
            params={},
            connection_id="c1",
            idempotency_key="k1",
        )
    )
    assert r.ok and r.data["mode"] == "inline"


@pytest.fixture
async def worker_server() -> Any:
    app = create_connector_worker_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_rpc_execute_roundtrip(worker_server: str) -> None:
    w = RpcConnectorWorker(worker_server)
    r = await w.execute(WorkerJob("rest", "ping", {"x": 1}, "c1", "k1"))
    assert r.ok
    assert r.data["echo"] == "ping"
    assert r.data["mode"] == "inline"


@pytest.mark.asyncio
async def test_rpc_worker_down_maps_unavailable() -> None:
    """worker 挂掉 → DOWNSTREAM_UNAVAILABLE，不悬挂。"""
    w = RpcConnectorWorker("http://127.0.0.1:1", connect_timeout_s=0.5, read_timeout_s=0.5)
    with pytest.raises(PlatformError) as ei:
        await w.execute(WorkerJob("rest", "ping", {}, "c1", "k1"))
    # 连接拒绝或短超时均不得悬挂；映射为下游类错误码
    assert ei.value.code in (ErrorCode.DOWNSTREAM_UNAVAILABLE, ErrorCode.DOWNSTREAM_TIMEOUT)


@pytest.mark.asyncio
async def test_rpc_custom_handler() -> None:
    """自定义 handler 经同一 RPC 协议返回。"""

    async def handler(job: WorkerJob) -> dict[str, Any]:
        return {"op": job.op, "params": job.params, "via": "custom"}

    app = create_connector_worker_app(handler=handler)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        w = RpcConnectorWorker(f"http://127.0.0.1:{port}")
        r = await w.execute(WorkerJob("sql", "query", {"q": "1"}, "c2", "k2"))
        assert r.ok and r.data == {"op": "query", "params": {"q": "1"}, "via": "custom"}
    finally:
        server.should_exit = True
        await task


def test_build_from_settings() -> None:
    assert build_connector_worker(Settings(connector_worker_mode="inline")).mode == "inline"
    assert isinstance(
        build_connector_worker(Settings(connector_worker_mode="rpc", connector_worker_url="http://x")),
        RpcConnectorWorker,
    )


def test_connector_worker_doc_and_k8s_draft() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "docs" / "ops" / "connector-worker.md").is_file()
    assert (root / "deploy" / "k8s" / "base" / "connector-worker.yaml").is_file()
